"""Gumloop proxy — WebSocket chat with per-account auth and Turnstile captcha.

Routes gl-* model requests through Gumloop's WebSocket API.
Each account has its own GumloopAuth instance. Turnstile solver is shared.

MCP Mode: Agent uses MCP tools server-side for file operations.
Client tools from OpenCode are stripped — Gumloop handles everything via MCP.
All tool events (tool-call, tool-result) are streamed as text content so
the client can see what the agent is doing.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import AsyncIterator

from fastapi.responses import StreamingResponse, JSONResponse

from .gumloop.auth import GumloopAuth
from .gumloop.turnstile import TurnstileSolver
from .gumloop.client import (
    send_chat,
    update_gummie_config,
    upload_file,
)
import base64
import re
import httpx as _httpx

from .gumloop.parser import build_openai_chunk, build_openai_done, build_openai_tool_call_chunk
from .gumloop.tool_converter import convert_messages_with_tools, parse_tool_calls

log = logging.getLogger("unified.proxy_gumloop")

# Auth cache: account_id → GumloopAuth
_auth_cache: dict[int, GumloopAuth] = {}

# Session cache: account_id → session_id (for persistent chat sessions)
_session_cache: dict[int, int] = {}

# Shared turnstile solver (tokens are account-independent)
_turnstile: TurnstileSolver | None = None

# Gumloop models (native names)
GUMLOOP_MODELS = [
    "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6",
    "claude-sonnet-4-5", "claude-haiku-4-5",
    "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano",
    "gpt-5.3-code", "gpt-5.2", "gpt-5.2-codex",
]


def _get_turnstile() -> TurnstileSolver:
    """Get or create the shared TurnstileSolver."""
    global _turnstile
    if _turnstile is None:
        api_key = os.getenv("CAPTCHA_API_KEY", "")
        _turnstile = TurnstileSolver(api_key)
    return _turnstile


async def _ensure_turnstile_key() -> None:
    """Load captcha API key from DB settings if not already set."""
    ts = _get_turnstile()
    if ts._api_key:
        return
    from . import database as db
    key = await db.get_setting("captcha_api_key", "")
    if key:
        ts.update_api_key(key)


def _get_auth(account: dict) -> GumloopAuth:
    """Get or create a GumloopAuth for an account."""
    acct_id = account["id"]
    if acct_id in _auth_cache:
        auth = _auth_cache[acct_id]
        db_token = account.get("gl_id_token", "")
        db_refresh = account.get("gl_refresh_token", "")
        if db_refresh and db_refresh != auth.refresh_token:
            auth.refresh_token = db_refresh
        if db_token and db_token != auth.id_token:
            auth.id_token = db_token
            auth.expires_at = 0
        return auth

    auth = GumloopAuth(
        refresh_token=account.get("gl_refresh_token", ""),
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
    )
    _auth_cache[acct_id] = auth
    return auth


def _map_gl_model(model: str) -> str:
    """Map gl-prefixed model to Gumloop's internal name."""
    bare = model.removeprefix("gl-")
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    if bare in GUMLOOP_MODELS:
        return bare
    return bare


def _extract_system_prompt(messages: list[dict]) -> str:
    """Extract system prompt from messages list."""
    parts = []
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
    return "\n\n".join(p for p in parts if p)


def _openai_tools_to_gumloop(tools: list[dict]) -> list[dict]:
    """Convert OpenAI function-calling tools to Gumloop's tool format."""
    result = []
    for tool in tools:
        if tool.get("type") != "function":
            continue
        func = tool.get("function", {})
        result.append({
            "name": func.get("name", ""),
            "description": func.get("description", ""),
            "input_schema": func.get("parameters", {"type": "object", "properties": {}}),
        })
    return result


def _tool_uses_to_openai(tool_uses: list[dict]) -> list[dict]:
    """Convert parsed tool_use blocks to OpenAI tool_calls format."""
    from .gumloop.tool_converter import fix_tool_args
    result = []
    for item in tool_uses:
        name = item.get("name", "")
        args = fix_tool_args(name, item.get("input", {}))
        result.append({
            "id": item.get("id", f"call_{uuid.uuid4().hex[:24]}"),
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(args, ensure_ascii=False),
            },
        })
    return result


def _safe_flush_point(text: str, start: int) -> int:
    """Find safe point to flush text without splitting a <tool_use> tag."""
    idx = text.find("<tool_use", start)
    if idx >= 0:
        return idx
    for prefix in ("<tool_us", "<tool_u", "<tool_", "<tool", "<too", "<to", "<t", "<"):
        if text.endswith(prefix):
            return len(text) - len(prefix)
    return len(text)


def _detect_media_type(data: bytes, fallback: str = "image/png") -> str:
    """Detect image media type from magic bytes."""
    if data[:8] == b'\x89PNG\r\n\x1a\n':
        return "image/png"
    if data[:2] == b'\xff\xd8':
        return "image/jpeg"
    if data[:4] == b'GIF8':
        return "image/gif"
    if data[:4] == b'RIFF' and data[8:12] == b'WEBP':
        return "image/webp"
    if data[:4] == b'%PDF':
        return "application/pdf"
    return fallback


def _ext_from_media_type(media_type: str) -> str:
    """Get file extension from media type."""
    m = {
        "image/png": "png", "image/jpeg": "jpg", "image/gif": "gif",
        "image/webp": "webp", "application/pdf": "pdf",
    }
    return m.get(media_type, "png")


async def _extract_image_data(image_url: str) -> tuple[bytes, str] | None:
    """Extract image bytes from OpenAI image_url format."""
    if image_url.startswith("data:"):
        match = re.match(r'data:([^;]+);base64,(.+)', image_url)
        if match:
            media_type = match.group(1)
            raw = base64.b64decode(match.group(2))
            return raw, media_type
        return None

    if image_url.startswith("http"):
        try:
            async with _httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(image_url)
                resp.raise_for_status()
                data = resp.content
                ct = resp.headers.get("content-type", "")
                media_type = ct.split(";")[0].strip() if ct else _detect_media_type(data)
                return data, media_type
        except Exception as e:
            log.warning("Failed to download image from %s: %s", image_url[:80], e)
            return None

    return None


def _convert_openai_messages_simple(body: dict) -> tuple[list[dict], str | None]:
    """Convert OpenAI messages to simple role/content format for Gumloop.

    Strips tools entirely — Gumloop uses MCP tools server-side.
    Converts tool role messages and tool_calls to plain text so conversation
    history is preserved even if client sent tool interactions.

    Returns (messages, system_prompt).
    """
    messages = body.get("messages", [])

    system_prompt = None
    result = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # System messages → extract as system prompt
        if role == "system":
            if isinstance(content, list):
                content = "\n".join(
                    p.get("text", "") for p in content
                    if isinstance(p, dict) and p.get("type") == "text"
                )
            system_prompt = (system_prompt + "\n" + content) if system_prompt else content
            continue

        # Tool role (tool results from client) → convert to user message
        if role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            tool_text = f"[Tool result for {tool_call_id}]: {content}" if content else ""
            if tool_text:
                result.append({"role": "user", "content": tool_text})
            continue

        # Assistant with tool_calls → convert to plain text
        tool_calls = msg.get("tool_calls")
        if role == "assistant" and tool_calls:
            parts = []
            if content:
                parts.append(content if isinstance(content, str) else str(content))
            for tc in tool_calls:
                func = tc.get("function", {})
                name = func.get("name", "?")
                args = func.get("arguments", "{}")
                parts.append(f"[Called tool: {name}({args})]")
            result.append({"role": "assistant", "content": "\n".join(parts)})
            continue

        # Handle content arrays (text + image_url blocks)
        images = []
        if isinstance(content, list):
            text_parts = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    text_parts.append(p.get("text", ""))
                elif p.get("type") == "image_url":
                    img_url = p.get("image_url", {})
                    url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
                    if url:
                        images.append(url)
            content = "\n".join(text_parts)

        msg_entry = {"role": role, "content": content or ""}
        if images:
            msg_entry["_images"] = images
        result.append(msg_entry)

    # Merge consecutive same-role messages (Gumloop requires strict alternation)
    merged = []
    for msg in result:
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] += "\n\n" + msg["content"]
            # Merge images if any
            if "_images" in msg:
                merged[-1].setdefault("_images", []).extend(msg["_images"])
        else:
            merged.append(msg)

    return merged, system_prompt


def _message_text_for_overlap(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


def _persisted_rows_to_openai_messages(rows: list[dict]) -> list[dict]:
    result = []
    for row in rows:
        role = row.get("role", "")
        if role not in ("system", "user", "assistant"):
            continue
        result.append({"role": role, "content": row.get("content", "")})
    return result


def _merge_persisted_and_current_messages(persisted: list[dict], current: list[dict]) -> list[dict]:
    if not persisted:
        return current
    if not current:
        return persisted

    def sig(msg: dict) -> tuple[str, str]:
        return (str(msg.get("role", "")), _message_text_for_overlap(msg.get("content", "")))

    max_overlap = min(len(persisted), len(current))
    overlap = 0
    for size in range(max_overlap, 0, -1):
        if [sig(m) for m in persisted[-size:]] == [sig(m) for m in current[:size]]:
            overlap = size
            break
    return persisted + current[overlap:]


def _render_transcript_context(messages: list[dict]) -> str:
    lines = [
        "You are continuing an existing conversation from the same OpenCode session.",
        "The transcript below is authoritative prior context. Continue naturally and consistently with it.",
        "",
        "<conversation_history>",
    ]
    for msg in messages:
        role = str(msg.get("role", "")).strip().upper() or "USER"
        content = _message_text_for_overlap(msg.get("content", ""))
        if not content:
            continue
        lines.append(f"{role}: {content}")
    lines.append("</conversation_history>")
    return "\n".join(lines)


def _render_delta_context(messages: list[dict]) -> str:
    lines = [
        "The following conversation occurred while you were not the active model. Use this as context to continue naturally:",
        "",
        "<delta_history>",
    ]
    for msg in messages:
        role = str(msg.get("role", "")).strip().upper() or "USER"
        content = _message_text_for_overlap(msg.get("content", ""))
        if not content:
            continue
        lines.append(f"{role}: {content}")
    lines.append("</delta_history>")
    return "\n".join(lines)


async def _rehydrate_openai_messages_if_needed(db, chat_session_id: int | None, account_id: int, current_messages: list[dict]) -> tuple[list[dict], dict]:
    """Returns (messages, rehydration_info) where rehydration_info has keys:
    - injected: bool
    - count: int (number of messages injected)
    - mode: str ("summary_delta" | "delta" | "none")
    """
    no_inject = (current_messages, {"injected": False, "count": 0, "mode": "none"})

    if not chat_session_id or not account_id:
        log.info("[REHYDRATE] skip: no session or account (session=%s account=%s)", chat_session_id, account_id)
        return no_inject

    binding = await db.get_gumloop_binding_full(chat_session_id, account_id)

    if binding:
        watermark = binding.get("last_synced_message_id")
        if watermark is None:
            latest_id = await db.get_latest_session_message_id(chat_session_id)
            if latest_id:
                await db.update_binding_watermark(chat_session_id, account_id, latest_id)
            log.info("[REHYDRATE] same-account legacy binding marked caught-up: session=%s account=%s", chat_session_id, account_id)
            return no_inject

        delta_rows = await db.get_session_messages_after(chat_session_id, watermark)
        if not delta_rows:
            log.info("[REHYDRATE] same-account no delta: session=%s account=%s watermark=%s", chat_session_id, account_id, watermark)
            return no_inject

        delta_messages = _persisted_rows_to_openai_messages(delta_rows)
        if not delta_messages:
            log.info("[REHYDRATE] same-account delta rows empty after filter: session=%s", chat_session_id)
            return no_inject

        context_message = {"role": "system", "content": _render_delta_context(delta_messages)}
        log.info("[REHYDRATE] same-account delta inject: session=%s account=%s watermark=%s count=%s", chat_session_id, account_id, watermark, len(delta_messages))
        return [context_message, *current_messages], {"injected": True, "count": len(delta_messages), "mode": "delta"}

    # --- First bind of a different account to this session ---
    # Use summary + bounded recent window instead of full raw history
    from .compactor import compact_messages, should_compact, _RECENT_WINDOW

    summary_row = await db.get_session_summary(chat_session_id)
    latest_id = await db.get_latest_session_message_id(chat_session_id)
    total_count = await db.get_session_message_count(chat_session_id)

    if total_count == 0:
        log.info("[REHYDRATE] first-bind but no stored messages: session=%s account=%s", chat_session_id, account_id)
        return no_inject

    summary_watermark = summary_row["watermark_message_id"] if summary_row else 0
    summary_text = summary_row["summary_text"] if summary_row else ""

    if total_count <= _RECENT_WINDOW and not summary_text:
        all_rows = await db.get_chat_messages(chat_session_id)
        summary_text = compact_messages(all_rows)
        await db.upsert_session_summary(chat_session_id, summary_text, latest_id, total_count)
        summary_watermark = latest_id
        log.info("[REHYDRATE] first-bind small-history compacted directly: session=%s watermark=%s msg_count=%s", chat_session_id, latest_id, total_count)

    # Large history: use summary + recent window
        if should_compact(total_count, summary_watermark, latest_id) or not summary_text:
            all_rows = await db.get_chat_messages(chat_session_id)
            summary_text = compact_messages(all_rows)
            await db.upsert_session_summary(chat_session_id, summary_text, latest_id, total_count)
            summary_watermark = latest_id
        log.info("[REHYDRATE] regenerated summary: session=%s watermark=%s msg_count=%s summary_len=%s", chat_session_id, summary_watermark, total_count, len(summary_text))

    recent_rows = await db.get_session_messages_between(chat_session_id, max(0, summary_watermark - _RECENT_WINDOW), limit=_RECENT_WINDOW)
    if not recent_rows:
        recent_rows = await db.get_session_messages_after(chat_session_id, max(0, latest_id - _RECENT_WINDOW))

    recent_messages = _persisted_rows_to_openai_messages(recent_rows) if recent_rows else []

    context_parts = []
    context_parts.append("You are continuing a conversation from the same OpenCode session but with a different upstream account.")
    context_parts.append("Below is a compacted summary of prior conversation followed by recent messages.")
    context_parts.append("")
    context_parts.append("<session_summary>")
    context_parts.append(summary_text)
    context_parts.append("</session_summary>")

    if recent_messages:
        context_parts.append("")
        context_parts.append("<recent_messages>")
        for msg in recent_messages:
            role = str(msg.get("role", "")).upper()
            content = _message_text_for_overlap(msg.get("content", ""))
            if content:
                context_parts.append(f"{role}: {content}")
        context_parts.append("</recent_messages>")

    context_message = {"role": "system", "content": "\n".join(context_parts)}
    inject_count = len(recent_messages) + 1
    log.info("[REHYDRATE] first-bind summary+recent inject: session=%s account=%s summary_len=%s recent=%s total_stored=%s",
             chat_session_id, account_id, len(summary_text), len(recent_messages), total_count)
    return [context_message, *current_messages], {"injected": True, "count": inject_count, "mode": "summary_delta"}


async def _update_rehydration_watermark(chat_session_id: int | None, account_id: int) -> None:
    if not chat_session_id or not account_id:
        return
    from . import database as db
    from .compactor import compact_messages, should_compact
    try:
        latest_id = await db.get_latest_session_message_id(chat_session_id)
        if latest_id:
            await db.update_binding_watermark(chat_session_id, account_id, latest_id)
            await db.update_chat_session(chat_session_id, last_gumloop_account_id=account_id)

        total_count = await db.get_session_message_count(chat_session_id)
        summary_row = await db.get_session_summary(chat_session_id)
        summary_watermark = summary_row["watermark_message_id"] if summary_row else 0

        if should_compact(total_count, summary_watermark, latest_id):
            all_rows = await db.get_chat_messages(chat_session_id)
            summary_text = compact_messages(all_rows)
            await db.upsert_session_summary(chat_session_id, summary_text, latest_id, total_count)
            log.info("[REHYDRATE] background summary update: session=%s watermark=%s msgs=%s",
                     chat_session_id, latest_id, total_count)
    except Exception as e:
        log.warning("Failed to update rehydration watermark for session=%s account=%s: %s", chat_session_id, account_id, e)


async def _get_or_create_session_for_account(account_id: int, db) -> int:
    """Get or create persistent chat session for account.
    
    Each account gets one persistent session that's reused across all chat requests.
    This ensures conversation context is maintained automatically.
    """
    if account_id in _session_cache:
        session_id = _session_cache[account_id]
        # Verify session still exists in database
        session = await db.get_chat_session(session_id)
        if session:
            return session_id
        # Session was deleted, remove from cache
        del _session_cache[account_id]

    session_id = await db.get_or_create_gumloop_session_for_account(account_id)
    _session_cache[account_id] = session_id
    log.info("Using persistent session %s for account %s", session_id, account_id)
    return session_id


async def proxy_chat_completions(
    body: dict,
    account: dict,
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy chat completion to Gumloop via WebSocket.

    MCP mode: strips client tools, agent uses MCP tools server-side.
    All tool activity is streamed as text content.
    
    Supports persistent chat sessions via 'chat_session_id' field in body.
    """
    from . import database as db
    
    auth = _get_auth(account)
    await _ensure_turnstile_key()
    turnstile = _get_turnstile()
    gummie_id = account.get("gl_gummie_id", "")

    if not gummie_id:
        return JSONResponse(
            {"error": {"message": "Account has no gummie_id", "type": "server_error"}},
            status_code=503,
        ), 0.0

    # Map model
    raw_model = body.get("model", "gl-claude-sonnet-4-5")
    gl_model = _map_gl_model(raw_model)

    # Detect if client sent tools (OpenCode agent mode)
    has_client_tools = bool(body.get("tools"))
    gumloop_tools = _openai_tools_to_gumloop(body.get("tools", [])) if has_client_tools else []

    if has_client_tools:
        # OpenCode mode: convert messages with tool context, preserve tool_use/tool_result
        system_prompt = _extract_system_prompt(body.get("messages", []))
        messages = convert_messages_with_tools(body.get("messages", []), tools=gumloop_tools, system=system_prompt)
    else:
        # Legacy MCP mode: strip tools, simple format
        messages, system_prompt = _convert_openai_messages_simple(body)

    account_id = account.get("id", 0)
    interaction_id = None
    chat_session_id = body.get("chat_session_id")

    existing_binding = ""
    session_id_int = 0
    if chat_session_id and account_id:
        try:
            session_id_int = int(chat_session_id)
            existing_binding = await db.get_gumloop_binding(session_id_int, account_id)
            if existing_binding:
                interaction_id = existing_binding
                log.info("Using existing interaction_id %s for session=%s account=%s", interaction_id, chat_session_id, account_id)
        except (ValueError, TypeError) as e:
            log.warning("Invalid chat_session_id '%s': %s", chat_session_id, e)

    messages, rehydration_info = await _rehydrate_openai_messages_if_needed(
        db,
        session_id_int if session_id_int else None,
        account_id,
        messages,
    )

    if not interaction_id and session_id_int and account_id:
        interaction_id = await db.get_or_create_gumloop_interaction_for_session_account(session_id_int, account_id)
        log.info("Created new interaction_id %s for session=%s account=%s", interaction_id, chat_session_id, account_id)

    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
        log.warning("Generated one-off interaction_id: %s (no session binding)", interaction_id)
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

    if not messages:
        return JSONResponse(
            {"error": {"message": "No messages provided", "type": "invalid_request_error"}},
            status_code=400,
        ), 0.0

    if has_client_tools:
        # OpenCode mode: pass system prompt as-is, no MCP rules injection
        config_system = system_prompt or None
    else:
        # Legacy MCP mode: prepend MCP rules to system prompt
        mcp_rules = (
            "You are a coding assistant. You have MCP tools connected to the user's LOCAL workspace.\n\n"
            "MANDATORY RULES (never violate):\n"
            "1. For ALL file operations: ONLY use MCP tools. NEVER use sandbox_python, sandbox_file, sandbox_download, or ANY sandbox tool.\n"
            "2. Sandbox tools run on a remote server, NOT the user's machine. MCP tools operate on the user's LOCAL filesystem.\n"
            "3. ALL output files (code, html, text) → write_file.\n"
            "4. ALL shell commands → bash.\n"
            "5. WORKSPACE: ALWAYS use RELATIVE paths (e.g. 'file.txt', 'folder/file.py'). NEVER use absolute paths like D:\\, C:\\, /root/, etc. The MCP workspace root is '.' — all files go there.\n"
            "6. IMAGE WORKFLOW (critical):\n"
            "   a. Generate image with image_generator tool → you get a response with storage_link (gl:// URL)\n"
            "   b. Immediately call download_file with the EXACT gl:// URL and a filename\n"
            "   c. Example: download_file(url=\"gl://uid-xxx/custom_agent_interactions/.../image.png\", filename=\"output.png\")\n"
            "   d. NEVER use sandbox_download. NEVER convert gl:// URLs to gumloop.com/files/ URLs.\n"
            "   e. The download_file MCP tool handles gl:// authentication internally.\n"
            "7. Respond in the same language as the user.\n"
            "8. FORGET any previous workspace paths from earlier sessions. Your workspace is '.' (current directory). Use list_directory('.') to see what's there.\n\n"
            "AVAILABLE MCP TOOLS:\n"
            "- File: read_file, write_file, edit_file, delete_file, rename_file, copy_file, file_info, read_image\n"
            "- Directory: list_directory, tree, create_directory\n"
            "- Search: glob_search, grep\n"
            "- Shell: bash, run_python\n"
            "- Git: git (run any git subcommand)\n"
            "- Network: http_request, download_file (supports gl:// and http/https)\n"
            "- Archive: zip_files, unzip_file\n"
            "- Text: diff, patch\n"
            "- Research: search_docs (library documentation via Context7), web_search (DuckDuckGo), fetch_url (read web pages), search_github_code (grep.app)\n\n"
            "IMPORTANT: To VIEW/ANALYZE images, use read_image (returns visual content you can see). Do NOT use read_file for images.\n"
        )
        config_system = f"{mcp_rules}\n{system_prompt}" if system_prompt else mcp_rules

    # Update gummie config
    # OpenCode mode: tools=[] disables Gumloop's native tool execution,
    # forcing it to act as pure LLM. Tool definitions are in system prompt
    # via convert_messages_with_tools, so LLM outputs <tool_use> XML that
    # we parse into OpenAI tool_calls for client-side execution.
    # Legacy MCP mode: tools=None preserves whatever tools are configured in Gumloop UI.
    config_tools = [] if has_client_tools else None
    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=config_system,
            tools=config_tools,
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("Failed to update gummie config: %s", e)

    # Persist refreshed tokens
    from . import database as db
    updated = auth.get_updated_tokens()
    if updated.get("gl_id_token"):
        try:
            await db.update_account(account["id"], **updated)
        except Exception:
            pass

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    if has_client_tools:
        if client_wants_stream:
            return _stream_gumloop_toolaware(
                gummie_id, messages, auth, turnstile, raw_model,
                stream_id, created, proxy_url,
                interaction_id=interaction_id,
                account_id=account.get("id", 0),
                account_email=account.get("email", "?"),
                chat_session_id=session_id_int,
                rehydration_info=rehydration_info,
            ), 0.0
        else:
            return await _accumulate_gumloop_toolaware(
                gummie_id, messages, auth, turnstile, raw_model,
                stream_id, created, proxy_url,
                interaction_id=interaction_id,
                chat_session_id=session_id_int,
                account_id=account.get("id", 0),
            )

    if client_wants_stream:
        return _stream_gumloop(
            gummie_id, messages, auth, turnstile, gl_model, raw_model,
            stream_id, created, proxy_url,
            interaction_id=interaction_id,
            account_id=account.get("id", 0),
            account_email=account.get("email", "?"),
            chat_session_id=session_id_int,
            rehydration_info=rehydration_info,
        ), 0.0
    else:
        return await _accumulate_gumloop(
            gummie_id, messages, auth, turnstile, gl_model, raw_model,
            stream_id, created, proxy_url,
            interaction_id=interaction_id,
            chat_session_id=session_id_int,
            account_id=account.get("id", 0),
        )


def _stream_gumloop_toolaware(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    display_model: str,
    stream_id: str,
    created: int,
    proxy_url: str | None,
    interaction_id: str | None = None,
    account_id: int = 0,
    account_email: str = "?",
    chat_session_id: int = 0,
    rehydration_info: dict | None = None,
) -> StreamingResponse:
    """Stream Gumloop response with proper OpenAI tool_calls parsing.

    Buffers text near <tool_use> tags, parses them at stream end,
    and emits OpenAI-compatible tool_calls SSE chunks so OpenCode
    can execute tools autonomously.

    All reasoning, tool-call, tool-result, and step events are streamed
    via reasoning_content so the user sees full process progress.
    """
    _stream_state = {
        "cost": 0.0,
        "content": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "done": False,
        "_account_id": account_id,
        "_account_email": account_email,
    }

    async def stream_sse() -> AsyncIterator[bytes]:
        try:
            full_text = ""
            streamed_pos = 0
            usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            buffering_notified = False

            yield build_openai_chunk(
                stream_id, display_model, content="", role="assistant", created=created,
            ).encode()

            async for event in send_chat(
                gummie_id, messages, auth, turnstile,
                interaction_id=interaction_id, proxy_url=proxy_url,
            ):
                etype = event.get("type", "")

                # --- Text content (buffered near <tool_use> tags) ---
                if etype == "text-delta":
                    delta = event.get("delta", "")
                    if delta:
                        full_text += delta
                        safe_until = _safe_flush_point(full_text, streamed_pos)
                        if safe_until > streamed_pos:
                            chunk_text = full_text[streamed_pos:safe_until]
                            yield build_openai_chunk(
                                stream_id, display_model, content=chunk_text, created=created,
                            ).encode()
                            streamed_pos = safe_until
                            buffering_notified = False
                        elif not buffering_notified and (len(full_text) - streamed_pos) > 10:
                            yield build_openai_chunk(
                                stream_id, display_model,
                                content="\n_Preparing tool call..._\n", created=created,
                            ).encode()
                            buffering_notified = True

                # --- Reasoning (actual LLM thinking only) ---
                elif etype == "reasoning-delta":
                    delta = event.get("delta", "")
                    if delta:
                        yield build_openai_chunk(
                            stream_id, display_model, reasoning_content=delta, created=created,
                        ).encode()

                # --- Tool call from Gumloop agent (streamed as visible content) ---
                elif etype == "tool-call":
                    tool_name = event.get("toolName", "?")
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
                        content=f"\n> **[Result]** `{tool_name}` →\n> ```\n> {preview}\n> ```\n", created=created,
                    ).encode()

                # --- Step boundary (visible progress) ---
                elif etype == "step-start":
                    yield build_openai_chunk(
                        stream_id, display_model,
                        content="\n---\n_Processing next step..._\n", created=created,
                    ).encode()

                # --- Error ---
                elif etype == "error":
                    error_msg = event.get("error", "Unknown Gumloop error")
                    error_type = event.get("errorType", "")
                    log.error("[GL stream-tool] error: %s (%s)", error_msg, error_type)

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
                    if not event.get("final", True):
                        yield build_openai_chunk(
                            stream_id, display_model,
                            content="\n_Agent processing..._\n", created=created,
                        ).encode()
                        continue
                    break

                # --- Keepalive ---
                elif etype == "keepalive":
                    yield b": keepalive\n\n"

            # Parse remaining buffered text for tool_use blocks
            unstreamed = full_text[streamed_pos:]
            remaining_text, tool_uses = parse_tool_calls(unstreamed)
            if remaining_text:
                yield build_openai_chunk(
                    stream_id, display_model, content=remaining_text, created=created,
                ).encode()

            # Emit tool_calls as proper OpenAI SSE chunks
            for idx, tc in enumerate(_tool_uses_to_openai(tool_uses)):
                yield build_openai_tool_call_chunk(
                    stream_id, display_model, idx,
                    tc["id"], tc["function"]["name"], tc["function"]["arguments"],
                    created=created,
                ).encode()

            if rehydration_info and rehydration_info.get("injected"):
                mode = rehydration_info.get("mode", "delta")
                count = rehydration_info.get("count", 0)
                status_msg = f"\n\n_[Context synced: {count} messages injected ({mode})]_"
                yield build_openai_chunk(stream_id, display_model, content=status_msg, created=created).encode()
                full_text += status_msg

            finish_reason = "tool_calls" if tool_uses else "stop"
            yield build_openai_chunk(
                stream_id, display_model,
                finish_reason=finish_reason, created=created, usage=usage,
            ).encode()
            yield build_openai_done().encode()

            _stream_state["content"] = full_text
            _stream_state["prompt_tokens"] = usage["prompt_tokens"]
            _stream_state["completion_tokens"] = usage["completion_tokens"]
            _stream_state["total_tokens"] = usage["total_tokens"]
            _stream_state["done"] = True

        except Exception as e:
            log.error("Gumloop tool-aware streaming error: %s", e, exc_info=True)
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
    resp._gl_stream_state = _stream_state  # type: ignore[attr-defined]
    return resp


async def _accumulate_gumloop_toolaware(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    display_model: str,
    stream_id: str,
    created: int,
    proxy_url: str | None,
    interaction_id: str | None = None,
    chat_session_id: int = 0,
    account_id: int = 0,
) -> tuple[JSONResponse, float]:
    """Accumulate Gumloop response with proper tool_calls parsing."""
    try:
        full_text = ""
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        async for event in send_chat(
            gummie_id, messages, auth, turnstile,
            interaction_id=interaction_id, proxy_url=proxy_url,
        ):
            etype = event.get("type", "")
            if etype == "text-delta":
                full_text += event.get("delta", "")
            elif etype == "finish":
                event_usage = event.get("usage") or {}
                prompt_tokens += event_usage.get("input_tokens", 0)
                completion_tokens += event_usage.get("output_tokens", 0)
                total_tokens += event_usage.get("total_tokens", 0)
                if event.get("final", True):
                    break
            elif etype == "error":
                return JSONResponse(
                    {"error": {"message": event.get("error", "Unknown Gumloop error"), "type": "proxy_error"}},
                    status_code=502,
                ), 0.0

        remaining_text, tool_uses = parse_tool_calls(full_text)
        tool_calls = _tool_uses_to_openai(tool_uses)
        message: dict = {"role": "assistant", "content": remaining_text or None}
        if tool_calls:
            message["tool_calls"] = tool_calls
        finish_reason = "tool_calls" if tool_calls else "stop"
        response = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": display_model,
            "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens, "total_tokens": total_tokens},
        }
        return JSONResponse(response, status_code=200), 0.0

    except Exception as e:
        log.error("Gumloop tool-aware non-streaming error: %s", e, exc_info=True)
        return JSONResponse(
            {"error": {"message": f"Gumloop error: {e}", "type": "proxy_error"}},
            status_code=502,
        ), 0.0


def _stream_gumloop(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    gl_model: str,
    display_model: str,
    stream_id: str,
    created: int,
    proxy_url: str | None,
    interaction_id: str | None = None,
    account_id: int = 0,
    account_email: str = "?",
    chat_session_id: int = 0,
    rehydration_info: dict | None = None,
) -> StreamingResponse:
    """Stream Gumloop response as OpenAI SSE chunks.

    All WS events (text, reasoning, tool-call, tool-result) are streamed
    as text content. Multi-step tool loops continue until final finish.
    """
    _stream_state = {
        "cost": 0.0,
        "content": "",
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "done": False,
        "_account_id": account_id,
        "_account_email": account_email,
    }

    async def stream_sse() -> AsyncIterator[bytes]:
        try:
            first_chunk = True
            full_text = ""

            def emit_text(text: str) -> bytes:
                nonlocal first_chunk, full_text
                if not text:
                    return b""
                full_text += text
                role_arg = "assistant" if first_chunk else None
                first_chunk = False
                return build_openai_chunk(
                    stream_id, display_model,
                    content=text, role=role_arg, created=created,
                ).encode()

            in_reasoning = False

            async for event in send_chat(
                gummie_id, messages, auth, turnstile,
                interaction_id=interaction_id, proxy_url=proxy_url,
            ):
                etype = event.get("type", "")
                # DEBUG: log every event type with full payload for errors
                if etype not in ("keepalive",):
                    if etype == "error":
                        log.warning("[GL stream] ERROR event: %s", json.dumps(event, ensure_ascii=False)[:500])
                    else:
                        delta_preview = str(event.get("delta", ""))[:50]
                        log.info("[GL stream] event: %s | delta: %s", etype, delta_preview)

                # ── Text content ──
                if etype == "text-delta":
                    delta = event.get("delta", "")
                    if delta:
                        # Close reasoning block if transitioning
                        if in_reasoning:
                            chunk = emit_text("\n\n")
                            if chunk:
                                yield chunk
                            in_reasoning = False
                        chunk = emit_text(delta)
                        if chunk:
                            yield chunk

                # ── Reasoning (stream as italic text so user sees progress) ──
                elif etype == "reasoning-start":
                    in_reasoning = True
                    chunk = emit_text("\n*Thinking:* ")
                    if chunk:
                        yield chunk

                elif etype == "reasoning-delta":
                    delta = event.get("delta", "")
                    if delta:
                        chunk = emit_text(delta)
                        if chunk:
                            yield chunk

                elif etype == "reasoning-end":
                    if in_reasoning:
                        chunk = emit_text("\n\n")
                        if chunk:
                            yield chunk
                        in_reasoning = False

                # ── Tool call started (show what agent is doing) ──
                elif etype == "tool-call":
                    tool_name = event.get("toolName", "?")
                    tool_input = event.get("input", {})
                    input_preview = json.dumps(tool_input, ensure_ascii=False)
                    if len(input_preview) > 300:
                        input_preview = input_preview[:300] + "..."
                    log.info("[GL stream] tool-call: %s(%s)", tool_name, input_preview[:100])
                    # Stream tool call as visible text so user sees progress
                    tool_text = f"\n\n> **[Tool]** `{tool_name}({input_preview})`\n"
                    chunk = emit_text(tool_text)
                    if chunk:
                        yield chunk

                # ── Tool result (show output) ──
                elif etype == "tool-result":
                    tool_name = event.get("toolName", "?")
                    output = event.get("output", "")
                    if isinstance(output, dict):
                        stdout = output.get("stdout", "")
                        stderr = output.get("stderr", "")
                        result_text = stdout or stderr or json.dumps(output, ensure_ascii=False)
                    elif isinstance(output, str):
                        result_text = output
                    else:
                        result_text = str(output)
                    log.info("[GL stream] tool-result: %s → %s", tool_name, result_text[:100])
                    # Stream result preview so user sees tool output
                    preview = result_text[:500]
                    if len(result_text) > 500:
                        preview += "..."
                    result_block = f"\n> **[Result]** `{tool_name}` →\n> ```\n> {preview}\n> ```\n\n"
                    chunk = emit_text(result_block)
                    if chunk:
                        yield chunk

                # ── Error from Gumloop ──
                elif etype == "error":
                    error_msg = event.get("error", "Unknown Gumloop error")
                    error_type = event.get("errorType", "")
                    log.error("[GL stream] Gumloop error: %s (%s)", error_msg, error_type)

                    # Mark account immediately (don't wait for BackgroundTask)
                    is_credit_error = "credit" in error_type.lower() or "credit" in error_msg.lower()
                    if is_credit_error:
                        _stream_state["error"] = f"CREDIT_EXHAUSTED: {error_msg}"
                        from . import database as _db
                        try:
                            acct_id = _stream_state.get("_account_id", 0)
                            await _db.mark_gl_exhausted_temporary(
                                acct_id, 3600,  # 1 hour cooldown
                                f"Credit exhausted: {error_msg[:150]}",
                            )
                            log.warning("[GL stream] Account %s credit exhausted — temp exhausted 1h",
                                        _stream_state.get("_account_email", "?"))
                            # Push to D1 immediately so sync doesn't overwrite
                            try:
                                from . import license_client as _lc
                                updated_acct = await _db.get_account(acct_id)
                                if updated_acct:
                                    await _lc.push_account_now(updated_acct)
                                    log.info("[GL stream] Pushed exhausted status to D1 for %s",
                                             _stream_state.get("_account_email", "?"))
                            except Exception:
                                pass
                        except Exception as db_err:
                            log.warning("[GL stream] Failed to mark exhausted: %s", db_err)
                    else:
                        _stream_state["error"] = error_msg

                    # Stream error as visible text to user
                    err_text = f"\n\n**[Gumloop Error]** {error_msg}\n"
                    chunk = emit_text(err_text)
                    if chunk:
                        yield chunk

                # ── Finish ──
                elif etype == "finish":
                    is_final = event.get("final", True)
                    usage = event.get("usage") or {}
                    _stream_state["prompt_tokens"] += usage.get("input_tokens", 0)
                    _stream_state["completion_tokens"] += usage.get("output_tokens", 0)
                    _stream_state["total_tokens"] += usage.get("total_tokens", 0)

                    if not is_final:
                        # Multi-step: agent is executing tools, more coming
                        continue

                    # Final finish — close the stream
                    if rehydration_info and rehydration_info.get("injected"):
                        mode = rehydration_info.get("mode", "delta")
                        count = rehydration_info.get("count", 0)
                        yield emit_text(f"\n\n_[Context synced: {count} messages injected ({mode})]_")

                    yield build_openai_chunk(
                        stream_id, display_model,
                        finish_reason="stop", created=created,
                        usage={
                            "prompt_tokens": _stream_state["prompt_tokens"],
                            "completion_tokens": _stream_state["completion_tokens"],
                            "total_tokens": _stream_state["total_tokens"],
                        },
                    ).encode()
                    yield build_openai_done().encode()
                    _stream_state["content"] = full_text
                    _stream_state["done"] = True
                    break

                # Ignore other events (step-start, keepalive, interaction-name-update, etc.)

        except Exception as e:
            log.error("Gumloop streaming error: %s", e, exc_info=True)
            err = {"error": {"message": str(e) or "Stream error", "type": "proxy_error"}}
            yield f"data: {json.dumps(err)}\n\n".encode()
            yield b"data: [DONE]\n\n"
            _stream_state["done"] = True

    resp = StreamingResponse(
        stream_sse(),
        status_code=200,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
    resp._gl_stream_state = _stream_state  # type: ignore[attr-defined]
    return resp


async def _accumulate_gumloop(
    gummie_id: str,
    messages: list[dict],
    auth: GumloopAuth,
    turnstile: TurnstileSolver,
    gl_model: str,
    display_model: str,
    stream_id: str,
    created: int,
    proxy_url: str | None,
    interaction_id: str | None = None,
    chat_session_id: int = 0,
    account_id: int = 0,
) -> tuple[JSONResponse, float]:
    """Accumulate Gumloop response into OpenAI chat.completion JSON."""
    try:
        full_text = []
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0

        async for event in send_chat(
            gummie_id, messages, auth, turnstile,
            interaction_id=interaction_id, proxy_url=proxy_url,
        ):
            etype = event.get("type", "")

            if etype == "text-delta":
                delta = event.get("delta", "")
                if delta:
                    full_text.append(delta)

            elif etype == "reasoning-delta":
                delta = event.get("delta", "")
                if delta:
                    full_text.append(delta)

            elif etype == "finish":
                usage = event.get("usage") or {}
                prompt_tokens += usage.get("input_tokens", 0)
                completion_tokens += usage.get("output_tokens", 0)
                total_tokens += usage.get("total_tokens", 0)
                if event.get("final", True):
                    break

        content = "".join(full_text)
        response = {
            "id": stream_id,
            "object": "chat.completion",
            "created": created,
            "model": display_model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": total_tokens,
            },
        }
        return JSONResponse(response, status_code=200), 0.0

    except Exception as e:
        log.error("Gumloop non-streaming error: %s", e, exc_info=True)
        return JSONResponse(
            {"error": {"message": f"Gumloop error: {e}", "type": "proxy_error"}},
            status_code=502,
        ), 0.0


def get_captcha_stats() -> dict:
    """Return captcha solve stats for dashboard display."""
    if _turnstile is None:
        return {"solved": 0, "errors": 0, "has_key": False}
    return {
        "solved": _turnstile.solve_count,
        "errors": _turnstile.solve_errors,
        "has_key": bool(_turnstile._api_key),
    }


async def close_all_clients() -> None:
    """Cleanup auth cache and turnstile solver."""
    global _auth_cache, _session_cache, _turnstile
    _auth_cache.clear()
    _session_cache.clear()
    if _turnstile:
        _turnstile.close()
        _turnstile = None
