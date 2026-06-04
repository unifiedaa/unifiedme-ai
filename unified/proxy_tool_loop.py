"""Proxy-native tool loop for GL2 models.

Intercepts tool_use from Gumloop, executes tools locally, feeds results back,
and only streams the final answer to OpenCode. OpenCode never sees tool chatter.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from .gumloop.client import send_chat, update_gummie_config
from .gumloop.parser import build_openai_chunk, build_openai_done
from .gumloop.tool_converter import (
    convert_messages_with_tools,
    fix_tool_args,
    parse_tool_calls,
    tools_to_system_prompt,
)
from .loop_tools import SUPPORTED_TOOLS, execute_tool
from .proxy_gumloop import _ensure_turnstile_key, _get_auth, _get_turnstile
from .proxy_gumloop_v2 import (
    LLM_ONLY_OVERRIDE,
    _compress_gl2_system_prompt,
    _extract_system,
    _filter_duplicate_mcp_tools,
    _map_gl2_model,
    _normalize_gl2_tools,
    _openai_tools_to_gumloop,
    _write_rehydration_log,
)

log = logging.getLogger("unified.proxy_tool_loop")

MAX_ITERATIONS = 15
MAX_INPUT_TOKENS = 150_000
MAX_OUTPUT_TOKENS = 32_000
MAX_CONSECUTIVE_FAILURES = 3
KEEPALIVE_INTERVAL = 5.0


def _contains_tool_xml(text: str) -> bool:
    return bool(text and re.search(r"<tool_use\b|</tool_use>", text, re.IGNORECASE))


def _tool_xml_blocked_message(stage: str) -> str:
    return (
        f"\n[Internal tool-call parsing guard triggered during {stage}. "
        "Tool XML was withheld instead of being streamed raw. "
        "Please retry the request.]"
    )


async def proxy_chat_completions(
    body: dict[str, Any],
    account: dict[str, Any],
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """GL2 proxy path with server-side local tool execution.

    Used for /v1/chat/completions when client sends tools for gl2-* models.
    """
    from . import database as db
    from .proxy_gumloop import _rehydrate_openai_messages_if_needed

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

    gumloop_tools = _openai_tools_to_gumloop(body.get("tools", []))
    gumloop_tools = _filter_duplicate_mcp_tools(gumloop_tools)
    gumloop_tools = _normalize_gl2_tools(gumloop_tools)
    system_prompt = _compress_gl2_system_prompt(_extract_system(messages))
    converted_messages = convert_messages_with_tools(messages)

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
        except (TypeError, ValueError):
            session_id_int = 0

    rehydration_info = {"injected": False, "count": 0, "mode": "none"}
    rehydration_error = ""
    if session_id_int and account_id:
        try:
            session_row = await db.get_chat_session(session_id_int)
            last_account = int(session_row.get("last_gumloop_account_id", 0) or 0) if session_row else 0
            is_cross_account = last_account > 0 and last_account != account_id
            if is_cross_account:
                messages, rehydration_info = await _rehydrate_openai_messages_if_needed(
                    db, session_id_int, account_id, messages,
                )
                converted_messages = convert_messages_with_tools(messages)
        except Exception as e:
            rehydration_error = str(e)
            log.error("[GL2_TOOL_LOOP_REHYDRATE] failed: session=%s account=%s error=%s", session_id_int, account_id, e)

    _write_rehydration_log(session_id_int, account_id, rehydration_info, rehydration_error)
    if not interaction_id and session_id_int and account_id:
        interaction_id = await db.get_or_create_gumloop_interaction_for_session_account(session_id_int, account_id)
    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]

    tool_prompt = tools_to_system_prompt(gumloop_tools) if gumloop_tools else ""
    combined_system = system_prompt or ""
    if tool_prompt:
        combined_system = (combined_system + "\n\n" + tool_prompt) if combined_system else tool_prompt
    combined_system = LLM_ONLY_OVERRIDE + "\n\n" + combined_system

    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=combined_system or None,
            tools=[],
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("Failed to update GL2 tool-loop gummie config: %s", e)

    if client_wants_stream:
        resp = StreamingResponse(
            tool_loop_stream(
                gummie_id=gummie_id,
                messages=converted_messages,
                auth=auth,
                turnstile=turnstile,
                display_model=raw_model,
                interaction_id=interaction_id,
                proxy_url=proxy_url,
                gumloop_tools=gumloop_tools,
                system_prompt=combined_system,
                rehydration_info=rehydration_info,
            ),
            status_code=200,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
        )
        return resp, 0.0

    chunks: list[bytes] = []
    async for chunk in tool_loop_stream(
        gummie_id=gummie_id,
        messages=converted_messages,
        auth=auth,
        turnstile=turnstile,
        display_model=raw_model,
        interaction_id=interaction_id,
        proxy_url=proxy_url,
        gumloop_tools=gumloop_tools,
        system_prompt=combined_system,
        rehydration_info=rehydration_info,
    ):
        chunks.append(chunk)

    content_parts: list[str] = []
    usage: dict[str, Any] = {}
    for raw in chunks:
        text = raw.decode("utf-8", errors="replace").strip()
        if not text.startswith("data: ") or text == "data: [DONE]":
            continue
        try:
            payload = json.loads(text[6:])
        except json.JSONDecodeError:
            continue
        choice = (payload.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        if delta.get("content"):
            content_parts.append(delta["content"])
        if payload.get("usage"):
            usage = payload["usage"]

    response = {
        "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": raw_model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": "".join(content_parts).strip()},
            "finish_reason": "stop",
        }],
        "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }
    return JSONResponse(response, status_code=200), 0.0


class BlockedRemoteToolAttemptError(RuntimeError):
    def __init__(self, audit_message: str, tool_name: str):
        super().__init__(f"Forbidden remote tool execution detected: {tool_name}")
        self.audit_message = audit_message
        self.tool_name = tool_name


@dataclass
class LoopBudget:
    max_input: int = MAX_INPUT_TOKENS
    max_output: int = MAX_OUTPUT_TOKENS
    max_iterations: int = MAX_ITERATIONS
    cumulative_input: int = 0
    cumulative_output: int = 0
    iteration: int = 0

    def record(self, usage: dict[str, Any]) -> None:
        self.cumulative_input += usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        self.cumulative_output += usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        self.iteration += 1

    @property
    def exceeded(self) -> bool:
        return (
            self.cumulative_input >= self.max_input
            or self.cumulative_output >= self.max_output
            or self.iteration >= self.max_iterations
        )

    @property
    def summary(self) -> str:
        return (
            f"iterations={self.iteration}/{self.max_iterations} "
            f"input={self.cumulative_input}/{self.max_input} "
            f"output={self.cumulative_output}/{self.max_output}"
        )


def _format_tool_result_xml(tool_id: str, name: str, result: str) -> str:
    return f'<tool_result tool_use_id="{tool_id}" status="success">\n{result}\n</tool_result>'


def _format_tool_error_xml(tool_id: str, name: str, error: str) -> str:
    return f'<tool_result tool_use_id="{tool_id}" status="error">\n{error}\n</tool_result>'


async def _call_gumloop_collect(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: Any,
    turnstile: Any,
    interaction_id: str,
    proxy_url: str | None,
) -> tuple[str, dict[str, Any]]:
    """Call Gumloop and collect full response text + usage. Non-streaming."""
    full_text = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    async for event in send_chat(
        gummie_id=gummie_id,
        messages=messages,
        auth=auth,
        turnstile=turnstile,
        interaction_id=interaction_id,
        proxy_url=proxy_url,
    ):
        etype = event.get("type", "")
        if etype == "text-delta":
            full_text += event.get("delta", "")
        elif etype == "blocked_remote_tool_attempt":
            tool_name = str(event.get("toolName", "?"))
            tool_input = event.get("input", {})
            reason = str(event.get("reason", "Blocked remote tool attempt"))
            audit_message = (
                "\n\n[Gumloop blocked remote tool attempt]\n"
                f"tool: {tool_name}\n"
                f"input: {json.dumps(tool_input, ensure_ascii=False)}\n"
                f"reason: {reason}\n"
            )
            raise BlockedRemoteToolAttemptError(audit_message, tool_name)
        elif etype == "finish":
            event_usage = event.get("usage") or {}
            usage["prompt_tokens"] += event_usage.get("input_tokens", 0)
            usage["completion_tokens"] += event_usage.get("output_tokens", 0)
            usage["total_tokens"] += event_usage.get("total_tokens", 0)
            if event.get("final", True):
                break
        elif etype == "error":
            raise RuntimeError(event.get("error", "Unknown Gumloop error"))

    return full_text, usage


def _extract_thinking(text: str) -> tuple[str, str | None]:
    """Extract thinking blocks and return (clean_text, reasoning_text)."""
    thinking_parts = re.findall(r"<thinking>\n?(.*?)</thinking>", text, flags=re.DOTALL)
    reasoning = "\n".join(p.strip() for p in thinking_parts if p.strip()) or None
    clean = re.sub(r"<thinking>\n?.*?</thinking>\n?", "", text, flags=re.DOTALL)
    return clean, reasoning


def _visible_thinking_block(reasoning: str) -> str:
    text = reasoning.strip()
    if not text:
        return ""
    first_line, _, rest = text.partition("\n")
    header = f"\nThought: {first_line.strip()}"
    if rest.strip():
        return f"{header}\n\n{rest.strip()}\n"
    return header + "\n"


def _compact_tool_args(tool_input: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in tool_input.items():
        shown = value
        if isinstance(value, str):
            if key.lower().endswith(("path", "filepath", "file_path")):
                shown = os.path.basename(value.rstrip("/\\")) or value
            if len(shown) > 80:
                shown = shown[:77] + "..."
        elif isinstance(value, (list, dict)):
            shown = json.dumps(value, ensure_ascii=False)
            if len(shown) > 80:
                shown = shown[:77] + "..."
        parts.append(f"{key}={shown}")
    return ", ".join(parts)


def _visible_iteration_block(iteration: int, tool_uses: list[dict[str, Any]]) -> str:
    tool_lines: list[str] = [
        f"\nUsing {len(tool_uses)} tool(s) - iteration {iteration}"
    ]
    for index, tu in enumerate(tool_uses, start=1):
        tool_name = tu.get("name", "?")
        tool_input = tu.get("input", {})
        args_preview = _compact_tool_args(tool_input)
        line = f"  {index}. {tool_name}"
        if args_preview:
            line += f"({args_preview})"
        tool_lines.append(line)
    return "\n".join(tool_lines) + "\n"


def _visible_tool_result(tool_name: str, result: str, ok: bool) -> str:
    label = "done" if ok else "failed"
    preview = result.strip()
    preview = re.sub(r"\n{3,}", "\n\n", preview)
    if len(preview) > 700:
        preview = preview[:697] + "..."
    return f"\n{tool_name} -> {label}\n{preview}\n"


async def tool_loop_stream(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: Any,
    turnstile: Any,
    display_model: str,
    interaction_id: str,
    proxy_url: str | None,
    gumloop_tools: list[dict[str, Any]],
    system_prompt: str,
    rehydration_info: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """Run proxy-native tool loop. Yields SSE chunks for OpenCode."""

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    budget = LoopBudget()
    consecutive_failures = 0
    internal_messages: list[dict[str, Any]] = []
    total_tool_calls = 0

    yield build_openai_chunk(stream_id, display_model, role="assistant", created=created).encode()

    while not budget.exceeded:
        all_messages = messages + internal_messages

        try:
            raw_response, usage = await _call_gumloop_collect(
                gummie_id=gummie_id,
                messages=all_messages,
                auth=auth,
                turnstile=turnstile,
                interaction_id=interaction_id,
                proxy_url=proxy_url,
            )
        except BlockedRemoteToolAttemptError as e:
            log.error("[tool_loop] blocked remote tool attempt: %s", e.tool_name)
            yield build_openai_chunk(
                stream_id,
                display_model,
                content=e.audit_message,
                created=created,
            ).encode()
            yield build_openai_chunk(
                stream_id,
                display_model,
                content=(
                    "\nError: Gumloop attempted forbidden remote sandbox execution. "
                    "Request aborted before execution; only local OpenCode tools are allowed."
                ),
                created=created,
            ).encode()
            break
        except Exception as e:
            log.error("[tool_loop] Gumloop call failed: %s", e)
            yield build_openai_chunk(
                stream_id, display_model,
                content=f"\n\nError communicating with model: {e}",
                created=created,
            ).encode()
            break

        budget.record(usage)

        clean_text, reasoning = _extract_thinking(raw_response)
        remaining_text, tool_uses = parse_tool_calls(clean_text)

        if not tool_uses:
            if reasoning:
                yield build_openai_chunk(
                    stream_id, display_model,
                    content=_visible_thinking_block(reasoning),
                    created=created,
                ).encode()
            if remaining_text:
                if _contains_tool_xml(remaining_text):
                    log.warning("[tool_loop] parser missed tool XML during main loop; suppressing raw XML stream")
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content=_tool_xml_blocked_message("main loop"),
                        created=created,
                    ).encode()
                else:
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content=remaining_text,
                        created=created,
                    ).encode()
            break

        iteration_msg = f"[Executing {len(tool_uses)} tool(s) internally, iteration {budget.iteration + 1}...]"
        yield build_openai_chunk(
            stream_id, display_model,
            content=_visible_iteration_block(budget.iteration + 1, tool_uses),
            created=created,
        ).encode()

        internal_messages.append({"role": "assistant", "content": raw_response})

        tool_results: list[str] = []
        for tu in tool_uses:
            tool_name = tu.get("name", "")
            tool_id = tu.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
            tool_args = fix_tool_args(tool_name, tu.get("input", {}))
            total_tool_calls += 1

            if tool_name not in SUPPORTED_TOOLS:
                error_text = f"Unknown tool: {tool_name}"
                result_xml = _format_tool_error_xml(tool_id, tool_name, error_text)
                tool_results.append(result_xml)
                yield build_openai_chunk(
                    stream_id,
                    display_model,
                    content=_visible_tool_result(tool_name, error_text, ok=False),
                    created=created,
                ).encode()
                consecutive_failures += 1
                continue

            log.info("[tool_loop] iter=%d executing %s(%s)",
                     budget.iteration, tool_name, json.dumps(tool_args, ensure_ascii=False)[:200])

            result = await execute_tool(tool_name, tool_args)

            if result.startswith("ERROR:"):
                tool_results.append(_format_tool_error_xml(tool_id, tool_name, result))
                yield build_openai_chunk(
                    stream_id,
                    display_model,
                    content=_visible_tool_result(tool_name, result, ok=False),
                    created=created,
                ).encode()
                consecutive_failures += 1
            else:
                tool_results.append(_format_tool_result_xml(tool_id, tool_name, result))
                yield build_openai_chunk(
                    stream_id,
                    display_model,
                    content=_visible_tool_result(tool_name, result, ok=True),
                    created=created,
                ).encode()
                consecutive_failures = 0

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.warning("[tool_loop] %d consecutive failures, aborting", consecutive_failures)
                break

        internal_messages.append({"role": "user", "content": "\n".join(tool_results)})

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            internal_messages.append({
                "role": "user",
                "content": "SYSTEM: Multiple tool failures. Provide your best answer with available information. Do not use more tools.",
            })
            try:
                final_text, final_usage = await _call_gumloop_collect(
                    gummie_id=gummie_id,
                    messages=messages + internal_messages,
                    auth=auth,
                    turnstile=turnstile,
                    interaction_id=interaction_id,
                    proxy_url=proxy_url,
                )
                budget.record(final_usage)
                clean_final, reasoning_final = _extract_thinking(final_text)
                final_remaining, final_tool_uses = parse_tool_calls(clean_final)
                if reasoning_final:
                    yield build_openai_chunk(stream_id, display_model, content=_visible_thinking_block(reasoning_final), created=created).encode()
                if final_tool_uses:
                    log.warning("[tool_loop] final failure fallback returned tool XML; suppressing raw XML stream")
                    yield build_openai_chunk(stream_id, display_model, content=_tool_xml_blocked_message("failure fallback"), created=created).encode()
                elif final_remaining:
                    if _contains_tool_xml(final_remaining):
                        log.warning("[tool_loop] parser left raw tool XML during failure fallback; suppressing stream")
                        yield build_openai_chunk(stream_id, display_model, content=_tool_xml_blocked_message("failure fallback"), created=created).encode()
                    else:
                        yield build_openai_chunk(stream_id, display_model, content=final_remaining, created=created).encode()
            except Exception as e:
                yield build_openai_chunk(stream_id, display_model, content=f"\nFailed to get final answer: {e}", created=created).encode()
            break

    else:
        log.warning("[tool_loop] Budget exceeded: %s", budget.summary)
        internal_messages.append({
            "role": "user",
            "content": "SYSTEM: Token budget exceeded. Provide your best answer now with available information. Do not use any more tools.",
        })
        try:
            final_text, final_usage = await _call_gumloop_collect(
                gummie_id=gummie_id,
                messages=messages + internal_messages,
                auth=auth,
                turnstile=turnstile,
                interaction_id=interaction_id,
                proxy_url=proxy_url,
            )
            budget.record(final_usage)
            clean_final, reasoning_final = _extract_thinking(final_text)
            final_remaining, final_tool_uses = parse_tool_calls(clean_final)
            if reasoning_final:
                yield build_openai_chunk(stream_id, display_model, content=_visible_thinking_block(reasoning_final), created=created).encode()
            if final_tool_uses:
                log.warning("[tool_loop] budget fallback returned tool XML; suppressing raw XML stream")
                yield build_openai_chunk(stream_id, display_model, content=_tool_xml_blocked_message("budget fallback"), created=created).encode()
            elif final_remaining:
                if _contains_tool_xml(final_remaining):
                    log.warning("[tool_loop] parser left raw tool XML during budget fallback; suppressing stream")
                    yield build_openai_chunk(stream_id, display_model, content=_tool_xml_blocked_message("budget fallback"), created=created).encode()
                else:
                    yield build_openai_chunk(stream_id, display_model, content=final_remaining, created=created).encode()
        except Exception as e:
            yield build_openai_chunk(stream_id, display_model, content=f"\nBudget exceeded, failed final call: {e}", created=created).encode()

    if rehydration_info and rehydration_info.get("injected"):
        mode = rehydration_info.get("mode", "delta")
        count = rehydration_info.get("count", 0)
        status_msg = f"\n\n_[Context synced: {count} messages injected ({mode})]_"
        yield build_openai_chunk(stream_id, display_model, content=status_msg, created=created).encode()

    if total_tool_calls > 0:
        loop_summary = f"\n\n_[Proxy tool loop: {total_tool_calls} tool calls across {budget.iteration} iterations, {budget.cumulative_input + budget.cumulative_output} internal tokens]_"
        yield build_openai_chunk(stream_id, display_model, content=loop_summary, created=created).encode()

    total_usage = {
        "prompt_tokens": budget.cumulative_input,
        "completion_tokens": budget.cumulative_output,
        "total_tokens": budget.cumulative_input + budget.cumulative_output,
    }
    yield build_openai_chunk(stream_id, display_model, finish_reason="stop", created=created, usage=total_usage).encode()
    yield build_openai_done().encode()
