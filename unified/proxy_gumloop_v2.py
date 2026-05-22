from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from .gumloop.client import send_chat, update_gummie_config, upload_file
from .gumloop.parser import build_openai_chunk, build_openai_done, build_openai_tool_call_chunk
from .gumloop.tool_converter import convert_messages_simple, parse_tool_calls, tools_to_system_prompt
from .proxy_gumloop import (
    _ensure_turnstile_key,
    _ext_from_media_type,
    _extract_image_data,
    _get_auth,
    _get_turnstile,
    _rehydrate_openai_messages_if_needed,
)

log = logging.getLogger("unified.proxy_gumloop_v2")

LLM_ONLY_OVERRIDE = (
    "You are a pure LLM. The user runs a local agent (OpenCode) that owns ALL tool execution.\n"
    "Do NOT use any built-in Gumloop or sandbox tools (sandbox_shell, sandbox_python, sandbox_file, "
    "sandbox_match, sandbox_download, sandbox_upload, invoke_agent, add_server_awaiter, "
    "trigger_discovery, list_trigger_options, create_integration_trigger, manage_integration_trigger, "
    "create_schedule, manage_schedule, create_mcp_trigger).\n"
    "When you need a tool, output ONLY this exact XML inside the assistant message:\n"
    "<tool_use>\n<name>TOOL_NAME</name>\n<input>{\"param\": \"value\"}</input>\n</tool_use>\n"
    "Use only the tool names provided by the user-side runtime below. Do not narrate your reasoning, "
    "do not say 'I detect ... intent', do not say 'Let me ...'. Output the tool_use XML directly when "
    "you need a tool, otherwise reply normally."
)

_GL2_MODEL_ALIASES = {"kimi-k2.6": "moonshotai/kimi-k2.6"}
_MCP_STRIP_PREFIXES = ("cloudflare-mcp_",)
_TOOL_ROOT_KEY: dict[str, str] = {"question": "questions", "todowrite": "todos"}
_TOOL_NAME_ALIASES: dict[str, str] = {
    "sandbox_shell": "bash",
    "sandbox_python": "bash",
    "sandbox_file": "read",
    "sandbox_match": "grep",
    "sandbox_download": "read",
    "sandbox_upload": "read",
}
_GL2_TOOL_HINT = (
    "Use only these local tools when needed: bash, read, write, edit, glob, grep, lsp_diagnostics, "
    "lsp_find_references, lsp_goto_definition, lsp_prepare_rename, lsp_rename, lsp_symbols, question, todowrite. "
    "Do not call sandbox_shell, sandbox_python, sandbox_file, sandbox_match, sandbox_upload, or sandbox_download."
)


def _map_gl2_model(model: str) -> str:
    bare = model.removeprefix("gl2-")
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    return _GL2_MODEL_ALIASES.get(bare, bare)


def _extract_system(messages: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content", "")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
    return "\n\n".join(part for part in parts if part)


async def _prime_gumloop_events(event_iter: AsyncIterator[dict[str, Any]]) -> tuple[dict[str, Any] | None, AsyncIterator[dict[str, Any]]]:
    first_event = await anext(event_iter, None)

    async def chained() -> AsyncIterator[dict[str, Any]]:
        if first_event is not None:
            yield first_event
        async for event in event_iter:
            yield event

    return first_event, chained()


def _openai_tools_to_gumloop(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        result.append(
            {
                "name": func.get("name", ""),
                "description": func.get("description", ""),
                "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
            }
        )
    return result


def _filter_duplicate_mcp_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [tool for tool in tools if not any(tool.get("name", "").startswith(prefix) for prefix in _MCP_STRIP_PREFIXES)]


def _empty_usage() -> dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "cache_creation_input_tokens": 0,
        "uncached_prompt_tokens": 0,
    }


def _map_event_usage(event_usage: dict[str, Any]) -> dict[str, int]:
    prompt_tokens = int(event_usage.get("input_tokens", 0) or 0)
    completion_tokens = int(event_usage.get("output_tokens", 0) or 0)
    cached_tokens = int(event_usage.get("cache_read_input_tokens", event_usage.get("cached_tokens", 0)) or 0)
    cache_creation_input_tokens = int(event_usage.get("cache_creation_input_tokens", 0) or 0)
    total_tokens = int(event_usage.get("total_tokens", prompt_tokens + completion_tokens) or 0)
    uncached_prompt_tokens = max(prompt_tokens - cached_tokens, 0)
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached_tokens": cached_tokens,
        "cache_creation_input_tokens": cache_creation_input_tokens,
        "uncached_prompt_tokens": uncached_prompt_tokens,
    }


def _tool_uses_to_openai(tool_uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .gumloop.tool_converter import fix_tool_args

    result = []
    for item in tool_uses:
        raw_name = str(item.get("name", "") or "")
        name = _TOOL_NAME_ALIASES.get(raw_name, raw_name)
        args = fix_tool_args(name, item.get("input", {}))
        root_key = _TOOL_ROOT_KEY.get(name)
        if root_key and isinstance(args, dict) and root_key not in args:
            args = {root_key: [args]}
        result.append(
            {
                "id": item.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }
        )
    return result


def _strip_visible_tool_xml(text: str) -> str:
    text = re.sub(r"<tool_use(?:\s+id=\"[^\"]*\")?>.*?</tool_use>", "", text, flags=re.DOTALL)
    text = re.sub(r"<tool_result[^>]*>.*?</tool_result>", "", text, flags=re.DOTALL)
    text = re.sub(r"<read_result>.*?</read_result>", "", text, flags=re.DOTALL)
    text = re.sub(r"<glob_results>.*?</glob_results>", "", text, flags=re.DOTALL)
    text = re.sub(r"<grep_results>.*?</grep_results>", "", text, flags=re.DOTALL)
    text = re.sub(r"Response from tool_use:.*?(?=\n\S|$)", "", text, flags=re.DOTALL)
    return text.strip()


async def _process_message_images(messages: list[dict[str, Any]], auth: Any, interaction_id: str, proxy_url: str | None) -> None:
    for msg in messages:
        image_urls = msg.pop("_images", None)
        if not image_urls or msg.get("role") != "user":
            continue
        gl_parts = []
        for img_url in image_urls:
            try:
                result = await _extract_image_data(img_url)
                if not result:
                    continue
                img_data, media_type = result
                ext = _ext_from_media_type(media_type)
                file_info = await upload_file(
                    auth=auth,
                    file_data=img_data,
                    file_name=f"image.{ext}",
                    content_type=media_type,
                    interaction_id=interaction_id,
                    proxy_url=proxy_url,
                )
                part_id = f"part_{uuid.uuid4().hex[:20]}"
                gl_parts.append(
                    {
                        "id": part_id,
                        "type": "file",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                        "file": file_info,
                    }
                )
            except Exception as e:
                log.error("Image upload failed: %s", e, exc_info=True)
        if gl_parts:
            msg["_gl_parts"] = gl_parts


async def proxy_chat_completions(
    body: dict[str, Any],
    account: dict[str, Any],
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    from . import database as db

    auth = _get_auth(account)
    await _ensure_turnstile_key()
    turnstile = _get_turnstile()
    gummie_id = account.get("gl_gummie_id", "")
    if not gummie_id:
        return JSONResponse({"error": {"message": "Account has no gummie_id", "type": "server_error"}}, status_code=503), 0.0

    raw_model = body.get("model", "gl2-claude-sonnet-4-5")
    gl_model = _map_gl2_model(raw_model)
    messages = body.get("messages", [])
    if not messages:
        return JSONResponse({"error": {"message": "No messages provided", "type": "invalid_request_error"}}, status_code=400), 0.0

    gumloop_tools = _filter_duplicate_mcp_tools(_openai_tools_to_gumloop(body.get("tools", [])))
    account_id = account.get("id", 0)
    interaction_id = None
    chat_session_id = body.get("chat_session_id")
    session_id_int = 0
    if chat_session_id and account_id:
        try:
            session_id_int = int(chat_session_id)
            existing_binding = await db.get_gumloop_binding(session_id_int, account_id)
            if existing_binding:
                interaction_id = existing_binding
        except Exception:
            pass

    messages, rehydration_info = await _rehydrate_openai_messages_if_needed(db, session_id_int if session_id_int else None, account_id, messages)
    if not interaction_id and session_id_int and account_id:
        interaction_id = await db.get_or_create_gumloop_interaction_for_session_account(session_id_int, account_id)
    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]

    system_prompt = _extract_system(messages)
    converted_messages = convert_messages_simple(messages)
    has_client_tools = bool(body.get("tools"))
    config_tools = None
    combined_system = system_prompt or ""
    if has_client_tools:
        tool_prompt = tools_to_system_prompt(gumloop_tools) if gumloop_tools else ""
        prefix_parts = [LLM_ONLY_OVERRIDE]
        if tool_prompt:
            prefix_parts.append(tool_prompt)
        prefix = "\n\n".join(prefix_parts)
        combined_system = (prefix + "\n\n" + combined_system) if combined_system else prefix

    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=combined_system or None,
            tools=config_tools,
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("Failed to update Gumloop v2 gummie config: %s", e)

    await _process_message_images(converted_messages, auth, interaction_id, proxy_url)

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    if client_wants_stream:
        return _stream_gumloop_v2(
            gummie_id,
            converted_messages,
            auth,
            turnstile,
            raw_model,
            stream_id,
            created,
            interaction_id,
            proxy_url,
            account_id=account.get("id", 0),
            account_email=account.get("email", "?"),
            chat_session_id=session_id_int or None,
            rehydration_info=rehydration_info,
        ), 0.0
    return await _accumulate_gumloop_v2(
        gummie_id,
        converted_messages,
        auth,
        turnstile,
        raw_model,
        stream_id,
        created,
        interaction_id,
        proxy_url,
        chat_session_id=session_id_int or None,
        account_id=account_id,
    )


def _stream_gumloop_v2(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: Any,
    turnstile: Any,
    display_model: str,
    stream_id: str,
    created: int,
    interaction_id: str,
    proxy_url: str | None,
    account_id: int = 0,
    account_email: str = "?",
    chat_session_id: int | None = None,
    rehydration_info: dict[str, Any] | None = None,
    event_iter: AsyncIterator[dict[str, Any]] | None = None,
) -> StreamingResponse:
    _stream_state: dict[str, Any] = {
        "cost": 0.0,
        "content": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "credits": 0,
        "done": False,
        "_account_id": account_id,
        "_account_email": account_email,
    }

    async def stream_sse() -> AsyncIterator[bytes]:
        try:
            full_text = ""
            stream_buffer = ""
            tool_call_index = 0
            usage = _empty_usage()
            yield build_openai_chunk(stream_id, display_model, role="assistant", created=created).encode()
            source_iter = event_iter or send_chat(gummie_id, messages, auth, turnstile, interaction_id=interaction_id, proxy_url=proxy_url)
            async for event in source_iter:
                etype = event.get("type", "")
                if etype == "text-delta":
                    delta = event.get("delta", "")
                    if delta:
                        stream_buffer += delta
                        while stream_buffer:
                            tag_pos = stream_buffer.find("<")
                            if tag_pos == -1:
                                full_text += stream_buffer
                                yield build_openai_chunk(stream_id, display_model, content=stream_buffer, created=created).encode()
                                stream_buffer = ""
                                break
                            if tag_pos > 0:
                                visible = stream_buffer[:tag_pos]
                                full_text += visible
                                yield build_openai_chunk(stream_id, display_model, content=visible, created=created).encode()
                                stream_buffer = stream_buffer[tag_pos:]
                                continue

                            if stream_buffer.startswith("<tool_use"):
                                close = "</tool_use>"
                                end = stream_buffer.find(close)
                                if end == -1:
                                    break
                                block = stream_buffer[: end + len(close)]
                                _, tool_uses = parse_tool_calls(block)
                                for tc in _tool_uses_to_openai(tool_uses):
                                    yield build_openai_tool_call_chunk(
                                        stream_id,
                                        display_model,
                                        tool_call_index,
                                        tc["id"],
                                        tc["function"]["name"],
                                        tc["function"]["arguments"],
                                        created=created,
                                    ).encode()
                                    tool_call_index += 1
                                finish_usage = _empty_usage()
                                yield build_openai_chunk(
                                    stream_id,
                                    display_model,
                                    finish_reason="tool_calls",
                                    created=created,
                                    usage=finish_usage,
                                ).encode()
                                yield build_openai_done().encode()
                                logged_content = _strip_visible_tool_xml(re.sub(r"<thinking>\n?.*?</thinking>\n?", "", full_text, flags=re.DOTALL))
                                _stream_state["content"] = logged_content
                                _stream_state["prompt_tokens"] = finish_usage["prompt_tokens"]
                                _stream_state["completion_tokens"] = finish_usage["completion_tokens"]
                                _stream_state["total_tokens"] = finish_usage["total_tokens"]
                                _stream_state["cached_tokens"] = finish_usage["cached_tokens"]
                                _stream_state["credits"] = finish_usage.get("credits", 0)
                                _stream_state["done"] = True
                                return

                            if stream_buffer.startswith("<thinking>"):
                                close = "</thinking>"
                                end = stream_buffer.find(close)
                                if end == -1:
                                    break
                                stream_buffer = stream_buffer[end + len(close):]
                                continue

                            handled = False
                            for open_tag, close_tag in (
                                ("<tool_result", "</tool_result>"),
                                ("<read_result>", "</read_result>"),
                                ("<glob_results>", "</glob_results>"),
                                ("<grep_results>", "</grep_results>"),
                            ):
                                if stream_buffer.startswith(open_tag):
                                    end = stream_buffer.find(close_tag)
                                    if end == -1:
                                        handled = True
                                        break
                                    stream_buffer = stream_buffer[end + len(close_tag):]
                                    handled = True
                                    break
                            if handled:
                                if stream_buffer.startswith("<"):
                                    continue
                                break

                            if stream_buffer.startswith("Response from tool_use:"):
                                nl = stream_buffer.find("\n")
                                if nl == -1:
                                    break
                                stream_buffer = stream_buffer[nl + 1:]
                                continue

                            break
                elif etype == "reasoning-delta":
                    continue
                elif etype == "tool-call":
                    raw_tool_name = str(event.get("toolName", "?") or "?")
                    tool_name = _TOOL_NAME_ALIASES.get(raw_tool_name, raw_tool_name)
                    from .gumloop.tool_converter import fix_tool_args
                    tool_input = fix_tool_args(tool_name, event.get("input", {}))
                    tool_call_id = event.get("toolCallId") or f"call_{uuid.uuid4().hex[:20]}"
                    yield build_openai_tool_call_chunk(
                        stream_id,
                        display_model,
                        tool_call_index,
                        tool_call_id,
                        tool_name,
                        json.dumps(tool_input, ensure_ascii=False),
                        created=created,
                    ).encode()
                    tool_call_index += 1
                    yield build_openai_chunk(
                        stream_id,
                        display_model,
                        finish_reason="tool_calls",
                        created=created,
                        usage=usage,
                    ).encode()
                    yield build_openai_done().encode()
                    _stream_state["content"] = _strip_visible_tool_xml(re.sub(r"<thinking>\n?.*?</thinking>\n?", "", full_text, flags=re.DOTALL))
                    _stream_state["prompt_tokens"] = usage["prompt_tokens"]
                    _stream_state["completion_tokens"] = usage["completion_tokens"]
                    _stream_state["total_tokens"] = usage["total_tokens"]
                    _stream_state["cached_tokens"] = usage["cached_tokens"]
                    _stream_state["credits"] = usage.get("credits", 0)
                    _stream_state["done"] = True
                    return
                elif etype == "tool-result":
                    pass
                elif etype == "finish":
                    usage = _map_event_usage(event.get("usage") or {})
                    usage["credits"] = event.get("credits") or 0
                    if not event.get("final", True):
                        continue
                    break
                elif etype == "error":
                    error_msg = event.get("error", "Unknown Gumloop v2 error")
                    _stream_state["error"] = error_msg
                    err = {"error": {"message": error_msg, "type": "proxy_error"}}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
                    yield build_openai_done().encode()
                    _stream_state["done"] = True
                    return
                elif etype == "status":
                    continue
                elif etype == "turnstile_retry":
                    continue
                elif etype == "keepalive":
                    yield b": keepalive\n\n"

            if rehydration_info and rehydration_info.get("injected"):
                mode = rehydration_info.get("mode", "delta")
                count = rehydration_info.get("count", 0)
                status_msg = f"\n\n_[Context synced: {count} messages injected ({mode})]_"
                full_text += status_msg
            else:
                status_msg = ""

            if stream_buffer:
                tail = _strip_visible_tool_xml(stream_buffer)
                if tail:
                    full_text += tail
                    yield build_openai_chunk(stream_id, display_model, content=tail, created=created).encode()
                stream_buffer = ""

            if status_msg:
                yield build_openai_chunk(stream_id, display_model, content=status_msg, created=created).encode()
            finish_reason = "tool_calls" if tool_call_index > 0 else "stop"
            yield build_openai_chunk(stream_id, display_model, finish_reason=finish_reason, created=created, usage=usage).encode()
            yield build_openai_done().encode()
            logged_content = _strip_visible_tool_xml(re.sub(r"<thinking>\n?.*?</thinking>\n?", "", full_text, flags=re.DOTALL))
            if status_msg:
                logged_content = (logged_content + status_msg).strip()
            _stream_state["content"] = logged_content
            _stream_state["prompt_tokens"] = usage["prompt_tokens"]
            _stream_state["completion_tokens"] = usage["completion_tokens"]
            _stream_state["total_tokens"] = usage["total_tokens"]
            _stream_state["cached_tokens"] = usage["cached_tokens"]
            _stream_state["credits"] = usage.get("credits", 0)
            _stream_state["done"] = True
        except Exception as e:
            log.error("Gumloop v2 streaming error: %s", e, exc_info=True)
            _stream_state["error"] = str(e)
            err = {"error": {"message": str(e) or "Stream error", "type": "proxy_error"}}
            yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            _stream_state["done"] = True

    resp = StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
    setattr(resp, "_gl_stream_state", _stream_state)
    return resp


async def _accumulate_gumloop_v2(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: Any,
    turnstile: Any,
    display_model: str,
    stream_id: str,
    created: int,
    interaction_id: str,
    proxy_url: str | None,
    chat_session_id: int | None = None,
    account_id: int = 0,
    event_iter: AsyncIterator[dict[str, Any]] | None = None,
) -> tuple[JSONResponse, float]:
    try:
        full_text = ""
        usage = _empty_usage()
        tool_calls: list[dict[str, Any]] = []
        source_iter = event_iter or send_chat(gummie_id, messages, auth, turnstile, interaction_id=interaction_id, proxy_url=proxy_url)
        async for event in source_iter:
            etype = event.get("type", "")
            if etype == "text-delta":
                full_text += event.get("delta", "")
            elif etype == "tool-call":
                raw_tool_name = str(event.get("toolName", "?") or "?")
                tool_name = _TOOL_NAME_ALIASES.get(raw_tool_name, raw_tool_name)
                from .gumloop.tool_converter import fix_tool_args
                tool_input = fix_tool_args(tool_name, event.get("input", {}))
                tool_call_id = event.get("toolCallId") or f"call_{uuid.uuid4().hex[:20]}"
                tool_calls.append({
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_input, ensure_ascii=False),
                    },
                })
            elif etype == "finish":
                usage = _map_event_usage(event.get("usage") or {})
                usage["credits"] = event.get("credits") or 0
                if event.get("final", True):
                    break
            elif etype == "turnstile_retry":
                retry_after = int(event.get("retry_after", 2) or 2)
                return JSONResponse(
                    {"error": {"message": "Turnstile verification failed", "type": "rate_limit"}},
                    status_code=429,
                    headers={"Retry-After": str(retry_after)},
                ), 0.0
            elif etype == "error":
                return JSONResponse({"error": {"message": event.get("error", "Unknown Gumloop v2 error"), "type": "proxy_error"}}, status_code=502), 0.0

        thinking_parts = re.findall(r"<thinking>\n?(.*?)</thinking>", full_text, flags=re.DOTALL)
        reasoning_text = "\n".join(p.strip() for p in thinking_parts if p.strip()) or None
        clean_text = re.sub(r"<thinking>\n?.*?</thinking>\n?", "", full_text, flags=re.DOTALL)
        remaining_text, parsed_tool_uses = parse_tool_calls(clean_text)
        if not tool_calls:
            tool_calls = _tool_uses_to_openai(parsed_tool_uses)
        message: dict[str, Any] = {"role": "assistant", "content": remaining_text or None}
        if reasoning_text:
            message["reasoning_content"] = reasoning_text
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish_reason = "tool_calls" if tool_calls else "stop"
        response = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": display_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": usage,
        }
        return JSONResponse(response), 0.0
    except Exception as e:
        log.error("Gumloop v2 error: %s", e, exc_info=True)
        return JSONResponse({"error": {"message": str(e) or "Unknown Gumloop v2 error", "type": "proxy_error"}}, status_code=502), 0.0
