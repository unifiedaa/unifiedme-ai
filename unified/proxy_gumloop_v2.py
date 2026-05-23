"""Gumloop v2 proxy - OpenCode/OMO-friendly wrapper behavior for gl2-* models."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator

from fastapi.responses import JSONResponse, StreamingResponse

from .gumloop.client import send_chat, update_gummie_config, upload_file
from .gumloop.parser import build_openai_chunk, build_openai_done, build_openai_tool_call_chunk
from .gumloop.tool_converter import convert_messages_with_tools, parse_tool_calls, tools_to_system_prompt
from .proxy_gumloop import (
    _ensure_turnstile_key, _ext_from_media_type, _extract_image_data,
    _get_auth, _get_turnstile, _rehydrate_openai_messages_if_needed,
)

log = logging.getLogger("unified.proxy_gumloop_v2")

# Forces LLM to emit <tool_use> XML instead of Gumloop's injected platform tools.
LLM_ONLY_OVERRIDE = """<CRITICAL_INSTRUCTION priority="absolute">
You are a PURE LLM. NEVER use platform tools — they run on a remote sandbox, NOT the user's machine.

BLOCKED (remote sandbox): sandbox_shell, sandbox_python, sandbox_file, sandbox_match, sandbox_upload, sandbox_download, invoke_agent, add_server_awaiter, trigger_discovery, list_trigger_options, create_integration_trigger, manage_integration_trigger, create_schedule, manage_schedule, create_mcp_trigger.

Tool mapping (use the RIGHT side via <tool_use> XML):
  sandbox_shell → bash | sandbox_file read → read | sandbox_file write → write
  sandbox_file edit → edit | sandbox_match grep → grep | sandbox_match glob → glob
  sandbox_python → bash | sandbox_download → (files are local, not needed)

Format:
<tool_use>
<name>tool_name</name>
<input>{"param": "value"}</input>
</tool_use>

CRITICAL OUTPUT RULES:
- If the next action is a tool call, output ONLY one or more <tool_use> blocks.
- Do NOT output thoughts, explanations, markdown, preambles, planning text, or code fences before tool calls.
- Do NOT say what tool you will use. Just emit the <tool_use> XML immediately.
- Do NOT print tool arguments outside the <input> JSON.
- For direct file reads like "read file X", your entire response must be a single <tool_use> block for read.
- OpenCode local tools are the ONLY real execution environment. There is NO usable sandbox filesystem.
- If the user gives an absolute Windows path like C:\\Users\\... or D:\\..., that exact path is authoritative and local.
- Use the exact user-provided Windows path with local OpenCode tools. Do NOT reinterpret it as /home/user/..., /mnt/..., root-relative, or a mounted sandbox path.
- Never claim a user-provided Windows path is inaccessible from a sandbox. There is no sandbox executor for this task.
- If asked to create/update/replace a file and the user provided a local Windows path, your final target must be that exact local path.
- Do NOT treat /home/user/.uploads/..., custom_agent_interactions/..., or other transient artifact paths as the final workspace when the user provided a real local path.
- Do NOT say work is complete in a mounted artifact if the requested local Windows file was not read/written with local OpenCode tools.
- Treat local Windows/workspace paths (for example C:\\... or relative project files) as readable with local tools.
- Treat provider artifact paths like /home/user/.uploads/... or custom_agent_interactions/... as NON-local transient artifacts.
- Never convert /home/user/.uploads/... into a Windows/local path. If you only have an artifact path, use the original source/result instead of local read.
- If a provider artifact path cannot be read with local tools, do NOT retry the same read via read/sandbox_file/bash. Explain that it is a remote artifact path and ask for the original/local source path or artifact contents.
- For remote URLs/repo paths: if a fetch/webfetch request returns 404 or path-not-found, do NOT repeat the same guessed path pattern. Pivot to listing/searching the parent location first, then retry only with evidence.

The proxy converts this XML into OpenAI tool_calls executed locally on the user's machine.
</CRITICAL_INSTRUCTION>
"""


# Aliases for models that need provider prefix on Gumloop
_GL2_MODEL_ALIASES = {
    "kimi-k2.6": "moonshotai/kimi-k2.6",
}

_GL2_EXPERIMENT_STABLE_TOOLS_ENV = "UNIFIED_GL2_EXPERIMENT_STABLE_TOOLS"
_GL2_EXPERIMENT_PRUNE_TOOLS_ENV = "UNIFIED_GL2_EXPERIMENT_PRUNE_TOOLS"
_GL2_EXPERIMENT_CACHE_CONFIG_ENV = "UNIFIED_GL2_EXPERIMENT_CACHE_CONFIG"
_GL2_WS_OPEN_TIMEOUT_ENV = "UNIFIED_GL2_WS_OPEN_TIMEOUT"
_GL2_WS_HANDSHAKE_RETRIES_ENV = "UNIFIED_GL2_WS_HANDSHAKE_RETRIES"
_GL2_DEBUG_PROMPT_BREAKDOWN_ENV = "UNIFIED_GL2_DEBUG_PROMPT_BREAKDOWN"
_GL2_COMPRESS_SYSTEM_PROMPT_ENV = "UNIFIED_GL2_COMPRESS_SYSTEM_PROMPT"
_GL2_TOOL_ALLOWLIST = {
    "read", "write", "edit", "grep", "glob", "bash", "todowrite", "question",
    "read_many", "list", "task", "fetch", "webfetch", "lsp_diagnostics",
    "lsp_find_references", "lsp_goto_definition", "lsp_rename", "lsp_symbols",
    "apply_patch", "skill",
}
# Tools that must NEVER pass through to OpenCode — dropped silently after remap attempt.
_GL2_BLOCKED_TOOLS = {
    "sandbox_upload", "sandbox_download",
    "invoke_agent", "add_server_awaiter",
    "trigger_discovery", "list_trigger_options",
    "create_integration_trigger", "manage_integration_trigger",
    "create_schedule", "manage_schedule", "create_mcp_trigger",
}
_gl2_config_cache: dict[str, str] = {}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _build_gl2_config_fingerprint(system_prompt: str, tools: list[dict[str, Any]] | None, model_name: str) -> str:
    payload = {
        "system_prompt": system_prompt,
        "tools": tools or [],
        "model_name": model_name,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _chars_json(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False))

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


# MCP tool prefixes to strip from tool injection.
# These are custom MCP servers whose tools duplicate standard OpenCode tools.
# In pure-LLM mode, OpenCode handles all tool execution locally, so the
# standard tools (read, grep, bash, etc.) are sufficient.
_MCP_STRIP_PREFIXES = ("cloudflare-mcp_",)


def _filter_duplicate_mcp_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove MCP-prefixed tools that duplicate standard OpenCode tools.

    Custom MCP tools (like cloudflare-mcp_read_file) are designed for
    Gumloop's native agent mode. In pure-LLM mode, OpenCode provides
    equivalent tools (read, grep, bash) that execute locally.
    Keeping both confuses the LLM into preferring the MCP variants.
    """
    return [
        tool for tool in tools
        if not any(tool.get("name", "").startswith(prefix) for prefix in _MCP_STRIP_PREFIXES)
    ]


def _normalize_gl2_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Optional gl2 experiment: keep tool prompt stable/cache-friendly."""
    if not (_env_flag(_GL2_EXPERIMENT_STABLE_TOOLS_ENV) or _env_flag(_GL2_EXPERIMENT_PRUNE_TOOLS_ENV)):
        return tools

    prune = _env_flag(_GL2_EXPERIMENT_PRUNE_TOOLS_ENV)
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for tool in sorted(tools, key=lambda t: str(t.get("name", ""))):
        name = str(tool.get("name", ""))
        if not name or name in seen:
            continue
        seen.add(name)
        if prune and name not in _GL2_TOOL_ALLOWLIST:
            continue
        normalized.append(tool)
    return normalized


_GL2_STRIP_SECTIONS = (
    "available_tools_you_can_use", "integration_execution_priority",
    "tool_usage_guidelines", "file_references", "available_skills",
    "execution_guidance", "interaction_etiquette", "platform_identity",
    "trigger_creation", "self_modification", "sandbox_environment",
    "agent_context", "execution_limits", "subagent_delegation",
    "file_exploration", "file_organization", "output_handling",
    "available_packages", "agent_workspace", "previously_generated_files",
    "gumcp_servers",
)
_GL2_STRIP_RE = re.compile(
    r"<(?:" + "|".join(_GL2_STRIP_SECTIONS) + r")>[\s\S]*?</(?:" + "|".join(_GL2_STRIP_SECTIONS) + r")>",
    re.IGNORECASE,
)


def _compress_gl2_system_prompt(prompt: str) -> str:
    if not prompt or not _env_flag(_GL2_COMPRESS_SYSTEM_PROMPT_ENV):
        return prompt
    original_len = len(prompt)
    compressed = _GL2_STRIP_RE.sub("", prompt)
    compressed = re.sub(r"\n{3,}", "\n\n", compressed)
    compressed = compressed.strip()
    if len(compressed) < original_len:
        log.info("[GL2_COMPRESS] system prompt %d -> %d chars (-%d%%)",
                 original_len, len(compressed),
                 int((1 - len(compressed) / original_len) * 100) if original_len else 0)
    return compressed


_TOOL_ROOT_KEY: dict[str, str] = {
    "question": "questions",
    "todowrite": "todos",
}

_GL2_ARTIFACT_PATH_PREFIXES = (
    "/home/user/.uploads/",
    "custom_agent_interactions/",
)


def _extract_tool_path(data: dict[str, Any]) -> str:
    return str(
        data.get("path")
        or data.get("filePath")
        or data.get("file")
        or data.get("file_path")
        or ""
    )


def _is_gl2_artifact_path(path: str) -> bool:
    p = path.strip()
    if not p:
        return False
    return p.startswith(_GL2_ARTIFACT_PATH_PREFIXES)


def _is_local_readable_path(path: str) -> bool:
    p = path.strip()
    if not p:
        return False
    if _is_gl2_artifact_path(p):
        return False
    if re.match(r"^[A-Za-z]:[\\/]", p):
        return True
    if p.startswith("\\\\"):
        return True
    if p.startswith("./") or p.startswith("../"):
        return True
    if p.startswith("/"):
        return False
    return True


def _artifact_path_tool_result(path: str, tool_name: str) -> dict[str, Any]:
    msg = (
        f"Remote artifact path cannot be executed with local tool '{tool_name}': {path}. "
        "This path belongs to Gumloop/provider transient storage, not the local filesystem. "
        "Need original local/source path or artifact contents instead of retrying local read/remap."
    )
    return {
        "id": f"call_{uuid.uuid4().hex[:24]}",
        "type": "function",
        "function": {
            "name": "question",
            "arguments": json.dumps(
                {
                    "questions": [
                        {
                            "header": "Artifact path blocked",
                            "multiple": False,
                            "question": msg,
                            "options": [
                                {
                                    "label": "Provide local path",
                                    "description": "Give original local/workspace path instead of Gumloop artifact path.",
                                },
                                {
                                    "label": "Provide contents",
                                    "description": "Paste artifact/file contents directly so work can continue without local read.",
                                },
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            ),
        },
    }


def _remap_gl2_remote_tool(name: str, args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    if name == "sandbox_file":
        action = str(args.get("action", "")).lower()
        mapped = {
            "read": "read",
            "view": "read",
            "write": "write",
            "append": "write",
            "edit": "edit",
            "str_replace": "edit",
        }.get(action)
        if mapped:
            path_value = _extract_tool_path(args)
            if mapped == "read" and path_value and not _is_local_readable_path(path_value):
                log.info("[GL2_TOOLS] Keeping sandbox_file read unremapped for non-local artifact path: %s", path_value[:200])
                return name, args
            cleaned = dict(args)
            cleaned.pop("action", None)
            cleaned.pop("content", None)
            cleaned.pop("old_string", None)
            cleaned.pop("new_string", None)
            cleaned.pop("replace_all", None)
            if mapped == "write" and "content" in args and "content" not in cleaned:
                cleaned["content"] = args.get("content")
            if mapped == "edit":
                if "old_string" in args:
                    cleaned["oldString"] = args.get("old_string")
                if "new_string" in args:
                    cleaned["newString"] = args.get("new_string")
                if "replace_all" in args:
                    cleaned["replaceAll"] = args.get("replace_all")
            return mapped, cleaned
    if name == "sandbox_shell":
        cleaned = dict(args)
        cleaned.pop("action", None)
        if "command" not in cleaned and "cmd" in cleaned:
            cleaned["command"] = cleaned.pop("cmd")
        return "bash", cleaned
    if name == "sandbox_match":
        action = str(args.get("action", "")).lower()
        cleaned = dict(args)
        cleaned.pop("action", None)
        if action == "grep":
            return "grep", cleaned
        if action == "glob":
            return "glob", cleaned
    if name == "sandbox_python":
        code = args.get("code", "")
        return "bash", {"command": f"python3 << 'PYEOF'\n{code}\nPYEOF", "description": "Execute Python code"}
    return name, args


def _tool_uses_to_openai(tool_uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    from .gumloop.tool_converter import fix_tool_args
    result = []
    for item in tool_uses:
        name = item.get("name", "")
        args = item.get("input", {})
        if isinstance(args, dict):
            name, args = _remap_gl2_remote_tool(name, args)
            tool_path = _extract_tool_path(args)
            if tool_path and _is_gl2_artifact_path(tool_path) and name in {"read", "write", "edit", "sandbox_file", "bash"}:
                log.warning("[GL2_TOOLS] Blocking artifact-path local execution: tool=%s path=%s", name, tool_path[:200])
                result.append(_artifact_path_tool_result(tool_path, name))
                continue
        if name in _GL2_BLOCKED_TOOLS:
            log.info("[GL2_TOOLS] Dropping blocked tool: %s", name)
            continue
        args = fix_tool_args(name, args)
        root_key = _TOOL_ROOT_KEY.get(name)
        if root_key and isinstance(args, dict) and root_key not in args:
            args = {root_key: [args]}
        result.append(
            {
                "id": item.get("id", f"call_{uuid.uuid4().hex[:24]}"),
                "type": "function",
                "function": {
                    "name": name,
                    "arguments": json.dumps(args, ensure_ascii=False),
                },
            }
        )
    return result


async def _process_message_images(
    messages: list[dict[str, Any]],
    auth,
    interaction_id: str,
    proxy_url: str | None,
) -> None:
    """Upload images from _images keys and attach as _gl_parts for Gumloop."""
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
                gl_parts.append({
                    "id": part_id,
                    "type": "file",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                    "file": file_info,
                })
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

    system_prompt = _compress_gl2_system_prompt(_extract_system(messages))
    gumloop_tools = _openai_tools_to_gumloop(body.get("tools", []))
    # Remove MCP-prefixed tools that duplicate standard OpenCode tools.
    # In pure-LLM mode, the LLM should use standard tool names (read, grep, bash)
    # instead of MCP variants (cloudflare-mcp_read_file, cloudflare-mcp_bash).
    gumloop_tools = _filter_duplicate_mcp_tools(gumloop_tools)
    gumloop_tools = _normalize_gl2_tools(gumloop_tools)
    if _env_flag(_GL2_EXPERIMENT_STABLE_TOOLS_ENV) or _env_flag(_GL2_EXPERIMENT_PRUNE_TOOLS_ENV):
        log.info(
            "[GL2_TOOLS] normalized tools: sent=%s stable=%s prune=%s",
            len(gumloop_tools),
            _env_flag(_GL2_EXPERIMENT_STABLE_TOOLS_ENV),
            _env_flag(_GL2_EXPERIMENT_PRUNE_TOOLS_ENV),
        )
    # History-only conversion: convert tool_use/tool_result blocks to plain text
    # Tool definitions go in system_prompt via update_gummie_config, NOT in messages
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
                log.info("Using existing interaction_id %s for session=%s account=%s", interaction_id, chat_session_id, account_id)
        except (TypeError, ValueError) as e:
            log.warning("Invalid chat_session_id '%s': %s", chat_session_id, e)

    messages, rehydration_info = await _rehydrate_openai_messages_if_needed(
        db,
        session_id_int if session_id_int else None,
        account_id,
        messages,
    )

    if not interaction_id and session_id_int and account_id:
        interaction_id = await db.get_or_create_gumloop_interaction_for_session_account(
            session_id_int, account_id
        )
        log.info("Created new interaction_id %s for session=%s account=%s", interaction_id, chat_session_id, account_id)
    system_prompt = _compress_gl2_system_prompt(_extract_system(messages))
    # Re-convert after rehydration, history-only (no system/tools injection)
    converted_messages = convert_messages_with_tools(messages)

    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
        log.warning("Generated one-off interaction_id: %s (no session binding)", interaction_id)

    # Disable Gumloop's native tool execution — act as pure LLM.
    # Tool definitions are embedded in system prompt via convert_messages_with_tools,
    # so LLM outputs <tool_use> XML that we parse into OpenAI tool_calls.
    has_client_tools = bool(body.get("tools"))
    config_tools = [] if has_client_tools else None

    # Build combined system prompt: LLM-only override + original system + tool definitions
    combined_system = system_prompt or ""
    tool_prompt = ""
    if has_client_tools and gumloop_tools:
        tool_prompt = tools_to_system_prompt(gumloop_tools)
        combined_system = (combined_system + "\n\n" + tool_prompt) if combined_system else tool_prompt
    # Prepend aggressive override to prevent Gumloop from using platform tools
    if has_client_tools:
        combined_system = LLM_ONLY_OVERRIDE + "\n\n" + combined_system

    if _env_flag(_GL2_DEBUG_PROMPT_BREAKDOWN_ENV):
        raw_messages = body.get("messages", [])
        log.info(
            "[GL2_PROMPT_BREAKDOWN] model=%s raw_messages_chars=%s rehydrated_messages_chars=%s converted_messages_chars=%s rehydration_added_chars=%s system_prompt_chars=%s llm_override_chars=%s tool_prompt_chars=%s combined_system_chars=%s tools_count=%s rehydration_injected=%s rehydration_mode=%s rehydration_count=%s",
            raw_model,
            _chars_json(raw_messages),
            _chars_json(messages),
            _chars_json(converted_messages),
            max(_chars_json(messages) - _chars_json(raw_messages), 0),
            len(system_prompt or ""),
            len(LLM_ONLY_OVERRIDE) if has_client_tools else 0,
            len(tool_prompt),
            len(combined_system or ""),
            len(gumloop_tools),
            bool(rehydration_info.get("injected")) if rehydration_info else False,
            rehydration_info.get("mode") if rehydration_info else "none",
            rehydration_info.get("count") if rehydration_info else 0,
        )

    should_update_config = True
    config_fingerprint = _build_gl2_config_fingerprint(combined_system, config_tools, gl_model)
    if _env_flag(_GL2_EXPERIMENT_CACHE_CONFIG_ENV):
        cached_fingerprint = _gl2_config_cache.get(gummie_id)
        if cached_fingerprint == config_fingerprint:
            should_update_config = False
            log.info("[GL2_CONFIG] Skip update_gummie_config fingerprint hit gummie=%s", gummie_id)

    try:
        if should_update_config:
            await update_gummie_config(
                gummie_id=gummie_id,
                auth=auth,
                system_prompt=combined_system or None,
                tools=config_tools,
                model_name=gl_model,
                proxy_url=proxy_url,
            )
            if _env_flag(_GL2_EXPERIMENT_CACHE_CONFIG_ENV):
                _gl2_config_cache[gummie_id] = config_fingerprint
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
            open_timeout=float(os.getenv(_GL2_WS_OPEN_TIMEOUT_ENV, os.getenv("GL_WS_OPEN_TIMEOUT", "30"))),
            handshake_retries=int(os.getenv(_GL2_WS_HANDSHAKE_RETRIES_ENV, os.getenv("GL_WS_HANDSHAKE_RETRIES", "3"))),
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
        open_timeout=float(os.getenv(_GL2_WS_OPEN_TIMEOUT_ENV, os.getenv("GL_WS_OPEN_TIMEOUT", "30"))),
        handshake_retries=int(os.getenv(_GL2_WS_HANDSHAKE_RETRIES_ENV, os.getenv("GL_WS_HANDSHAKE_RETRIES", "3"))),
    )


_CONTENT_PARTIAL_PREFIXES = (
    "<thinking", "<thinkin", "<thinki",
    "<tool_us", "<tool_u",
    "<think", "<tool_",
    "<thin", "<tool",
    "<thi", "<too",
    "<th", "<to",
    "<t", "<",
)

_THINKING_CLOSE_PREFIXES = (
    "</thinking", "</thinkin", "</thinki",
    "</think", "</thin", "</thi",
    "</th", "</t", "</",
)

_GL2_REMOTE_TOOL_EVENT_NAMES = {
    "sandbox_shell", "sandbox_python",
    "sandbox_upload", "sandbox_download",
    "invoke_agent", "add_server_awaiter",
    "trigger_discovery", "list_trigger_options",
    "create_integration_trigger", "manage_integration_trigger",
    "create_schedule", "manage_schedule", "create_mcp_trigger",
}


def _safe_flush_point(text: str, start: int) -> int:
    for tag in ("<tool_use", "<thinking>"):
        idx = text.find(tag, start)
        if idx >= 0:
            return idx
    for prefix in _CONTENT_PARTIAL_PREFIXES:
        if text.endswith(prefix) and len(text) - len(prefix) >= start:
            return len(text) - len(prefix)
    return len(text)


def _safe_thinking_flush(text: str, start: int) -> int:
    idx = text.find("</thinking>", start)
    if idx >= 0:
        return idx
    for prefix in _THINKING_CLOSE_PREFIXES:
        if text.endswith(prefix) and len(text) - len(prefix) >= start:
            return len(text) - len(prefix)
    return len(text)


def _stream_gumloop_v2(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth,
    turnstile,
    display_model: str,
    stream_id: str,
    created: int,
    interaction_id: str,
    proxy_url: str | None,
    account_id: int = 0,
    account_email: str = "?",
    chat_session_id: int | None = None,
    rehydration_info: dict[str, Any] | None = None,
    open_timeout: float | None = None,
    handshake_retries: int | None = None,
) -> StreamingResponse:
    _stream_state: dict[str, Any] = {
        "cost": 0.0,
        "content": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "uncached_prompt_tokens": 0,
        "credits": 0,
        "done": False,
        "_account_id": account_id,
        "_account_email": account_email,
    }

    async def stream_sse() -> AsyncIterator[bytes]:
        try:
            full_text = ""
            streamed_pos = 0
            in_thinking = False
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "uncached_prompt_tokens": 0,
                "credits": 0,
            }

            yield build_openai_chunk(stream_id, display_model, role="assistant", created=created).encode()

            async for event in send_chat(
                gummie_id,
                messages,
                auth,
                turnstile,
                interaction_id=interaction_id,
                proxy_url=proxy_url,
                open_timeout=open_timeout,
                handshake_retries=handshake_retries,
            ):
                etype = event.get("type", "")
                if etype not in ("keepalive",):
                    if etype == "error":
                        log.warning("[GL2 stream] ERROR event: %s", json.dumps(event, ensure_ascii=False)[:500])
                    else:
                        delta_preview = str(event.get("delta", ""))[:50]
                        log.info("[GL2 stream] event: %s | delta: %s", etype, delta_preview)

                if etype == "text-delta":
                    delta = event.get("delta", "")
                    if delta:
                        full_text += delta
                        while streamed_pos < len(full_text):
                            if not in_thinking:
                                tag_pos = full_text.find("<thinking>", streamed_pos)
                                if tag_pos >= 0:
                                    if tag_pos > streamed_pos:
                                        yield build_openai_chunk(stream_id, display_model, content=full_text[streamed_pos:tag_pos], created=created).encode()
                                    streamed_pos = tag_pos + len("<thinking>")
                                    if streamed_pos < len(full_text) and full_text[streamed_pos] == "\n":
                                        streamed_pos += 1
                                    in_thinking = True
                                    continue
                                safe_until = _safe_flush_point(full_text, streamed_pos)
                                if safe_until > streamed_pos:
                                    yield build_openai_chunk(stream_id, display_model, content=full_text[streamed_pos:safe_until], created=created).encode()
                                    streamed_pos = safe_until
                                break
                            else:
                                close_pos = full_text.find("</thinking>", streamed_pos)
                                if close_pos >= 0:
                                    if close_pos > streamed_pos:
                                        yield build_openai_chunk(stream_id, display_model, reasoning_content=full_text[streamed_pos:close_pos], created=created).encode()
                                    streamed_pos = close_pos + len("</thinking>")
                                    if streamed_pos < len(full_text) and full_text[streamed_pos] == "\n":
                                        streamed_pos += 1
                                    in_thinking = False
                                    continue
                                safe_until = _safe_thinking_flush(full_text, streamed_pos)
                                if safe_until > streamed_pos:
                                    yield build_openai_chunk(stream_id, display_model, reasoning_content=full_text[streamed_pos:safe_until], created=created).encode()
                                    streamed_pos = safe_until
                                break

                # --- Reasoning (streamed as reasoning_content) ---
                elif etype == "reasoning-delta":
                    delta = event.get("delta", "")
                    if delta:
                        yield build_openai_chunk(
                            stream_id, display_model, reasoning_content=delta, created=created,
                        ).encode()

                # --- Tool call from Gumloop agent (streamed as visible content) ---
                elif etype == "tool-call":
                    tool_name = event.get("toolName", "?")
                    if tool_name in _GL2_REMOTE_TOOL_EVENT_NAMES:
                        log.info("[GL2 stream] suppress remote tool-call echo: %s", tool_name)
                        continue
                    tool_input = event.get("input", {})
                    input_preview = json.dumps(tool_input, ensure_ascii=False)
                    if len(input_preview) > 200:
                        input_preview = input_preview[:200] + "..."
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content=f"\n> **[Tool]** `{tool_name}({input_preview})`\n", created=created,
                    ).encode()

                # --- Tool result from Gumloop agent (streamed as visible content) ---
                elif etype == "tool-result":
                    tool_name = event.get("toolName", "?")
                    if tool_name in _GL2_REMOTE_TOOL_EVENT_NAMES:
                        log.info("[GL2 stream] suppress remote tool-result echo: %s", tool_name)
                        continue
                    output = event.get("output", "")
                    if isinstance(output, dict):
                        result_text = output.get("stdout", "") or output.get("stderr", "") or json.dumps(output, ensure_ascii=False)
                    elif isinstance(output, str):
                        result_text = output
                    else:
                        result_text = str(output)
                    preview = result_text[:300] + "..." if len(result_text) > 300 else result_text
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content=f"\n> **[Result]** `{tool_name}` \u2192\n> ```\n> {preview}\n> ```\n", created=created,
                    ).encode()

                elif etype == "blocked_remote_tool_attempt":
                    tool_name = event.get("toolName", "?")
                    tool_input = event.get("input", {})
                    reason = event.get("reason", "Blocked remote tool attempt")
                    audit_text = (
                        "\n> **[Gumloop Attempt Blocked]**\n"
                        f"> tool: `{tool_name}`\n"
                        f"> input: ```json\n{json.dumps(tool_input, ensure_ascii=False)}\n```\n"
                        f"> reason: {reason}\n"
                    )
                    yield build_openai_chunk(
                        stream_id, display_model, content=audit_text, created=created,
                    ).encode()

                # --- Step boundary (skip — handled by reasoning flow) ---
                elif etype == "step-start":
                    pass

                # --- Error ---
                elif etype == "error":
                    error_msg = event.get("error", "Unknown Gumloop v2 error")
                    error_type = event.get("errorType", "")
                    log.error("[GL2 stream] error: %s (%s)", error_msg, error_type)

                    is_credit_error = "credit" in error_type.lower() or "credit" in error_msg.lower()
                    if is_credit_error:
                        _stream_state["error"] = f"CREDIT_EXHAUSTED: {error_msg}"
                        from . import database as _db
                        try:
                            acct_id = _stream_state.get("_account_id", 0)
                            await _db.mark_gl_exhausted_temporary(acct_id, 3600, f"Credit exhausted: {error_msg[:150]}")
                            try:
                                from . import license_client as _lc
                                updated_acct = await _db.get_account(acct_id)
                                if updated_acct:
                                    await _lc.push_account_now(updated_acct)
                            except Exception:
                                pass
                        except Exception:
                            pass
                    else:
                        _stream_state["error"] = error_msg

                    err = {"error": {"message": error_msg, "type": "proxy_error"}}
                    yield f"data: {json.dumps(err, ensure_ascii=False)}\n\n".encode()
                    yield build_openai_done().encode()
                    _stream_state["done"] = True
                    return

                # --- Finish ---
                elif etype == "finish":
                    event_usage = event.get("usage") or {}
                    usage["prompt_tokens"] += event_usage.get("input_tokens", 0)
                    usage["completion_tokens"] += event_usage.get("output_tokens", 0)
                    usage["total_tokens"] += event_usage.get("total_tokens", 0)
                    usage["cached_tokens"] += event_usage.get("cached_tokens", 0)
                    usage["uncached_prompt_tokens"] += event_usage.get(
                        "uncached_prompt_tokens",
                        event_usage.get("cache_creation_input_tokens", 0),
                    )
                    usage["credits"] += event.get("credits", 0) or 0
                    if not event.get("final", True):
                        continue
                    break

                # --- Keepalive ---
                elif etype == "keepalive":
                    yield b": keepalive\n\n"

            unstreamed = full_text[streamed_pos:]
            if in_thinking:
                thinking_leftover = unstreamed.replace("</thinking>", "").rstrip()
                if thinking_leftover:
                    yield build_openai_chunk(stream_id, display_model, reasoning_content=thinking_leftover, created=created).encode()
                unstreamed = ""
            unstreamed = re.sub(r"<thinking>\n?.*?</thinking>\n?", "", unstreamed, flags=re.DOTALL)
            remaining_text, tool_uses = parse_tool_calls(unstreamed)
            if remaining_text:
                yield build_openai_chunk(stream_id, display_model, content=remaining_text, created=created).encode()

            for idx, tc in enumerate(_tool_uses_to_openai(tool_uses)):
                yield build_openai_tool_call_chunk(
                    stream_id, display_model, idx,
                    tc["id"], tc["function"]["name"], tc["function"]["arguments"],
                    created=created,
                ).encode()

            finish_reason = "tool_calls" if tool_uses else "stop"
            yield build_openai_chunk(stream_id, display_model, finish_reason=finish_reason, created=created, usage=usage).encode()
            yield build_openai_done().encode()

            _stream_state["content"] = full_text
            _stream_state["prompt_tokens"] = usage["prompt_tokens"]
            _stream_state["completion_tokens"] = usage["completion_tokens"]
            _stream_state["total_tokens"] = usage["total_tokens"]
            _stream_state["cached_tokens"] = usage["cached_tokens"]
            _stream_state["uncached_prompt_tokens"] = usage["uncached_prompt_tokens"]
            _stream_state["credits"] = usage["credits"]
            _stream_state["done"] = True

        except Exception as e:
            log.error("Gumloop v2 streaming error: %s", e, exc_info=True)
            _stream_state["error"] = str(e)
            err = {"error": {"message": str(e) or "Stream error", "type": "proxy_error"}}
            yield f"data: {json.dumps(err)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            _stream_state["done"] = True

    resp = StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
    resp._gl_stream_state = _stream_state  # pyright: ignore[reportAttributeAccessIssue]
    return resp



async def _accumulate_gumloop_v2(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth,
    turnstile,
    display_model: str,
    stream_id: str,
    created: int,
    interaction_id: str,
    proxy_url: str | None,
    chat_session_id: int | None = None,
    account_id: int = 0,
    open_timeout: float | None = None,
    handshake_retries: int | None = None,
) -> tuple[JSONResponse, float]:
    try:
        full_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        cached_tokens = 0
        uncached_prompt_tokens = 0
        credits = 0
        async for event in send_chat(
            gummie_id,
            messages,
            auth,
            turnstile,
            interaction_id=interaction_id,
            proxy_url=proxy_url,
            open_timeout=open_timeout,
            handshake_retries=handshake_retries,
        ):
            etype = event.get("type", "")
            if etype == "text-delta":
                full_text += event.get("delta", "")
            elif etype == "blocked_remote_tool_attempt":
                tool_name = event.get("toolName", "?")
                tool_input = event.get("input", {})
                reason = event.get("reason", "Blocked remote tool attempt")
                full_text += (
                    "\n\n[Gumloop Attempt Blocked]\n"
                    f"tool: {tool_name}\n"
                    f"input: {json.dumps(tool_input, ensure_ascii=False)}\n"
                    f"reason: {reason}\n"
                )
            elif etype == "finish":
                event_usage = event.get("usage") or {}
                prompt_tokens += event_usage.get("input_tokens", 0)
                completion_tokens += event_usage.get("output_tokens", 0)
                total_tokens += event_usage.get("total_tokens", 0)
                cached_tokens += event_usage.get("cached_tokens", 0)
                uncached_prompt_tokens += event_usage.get(
                    "uncached_prompt_tokens",
                    event_usage.get("cache_creation_input_tokens", 0),
                )
                credits += event.get("credits", 0) or 0
                if event.get("final", True):
                    break
            elif etype == "error":
                return JSONResponse({"error": {"message": event.get("error", "Unknown Gumloop v2 error"), "type": "proxy_error"}}, status_code=502), 0.0

        thinking_parts = re.findall(r"<thinking>\n?(.*?)</thinking>", full_text, flags=re.DOTALL)
        reasoning_text = "\n".join(p.strip() for p in thinking_parts if p.strip()) or None
        clean_text = re.sub(r"<thinking>\n?.*?</thinking>\n?", "", full_text, flags=re.DOTALL)
        remaining_text, tool_uses = parse_tool_calls(clean_text)
        tool_calls = _tool_uses_to_openai(tool_uses)
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
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
                "cached_tokens": cached_tokens,
                "uncached_prompt_tokens": uncached_prompt_tokens,
                "credits": credits,
            },
        }
        return JSONResponse(response, status_code=200), 0.0
    except Exception as e:
        log.error("Gumloop v2 error: %s", e, exc_info=True)
        return JSONResponse({"error": {"message": f"Gumloop v2 error: {e}", "type": "proxy_error"}}, status_code=502), 0.0
