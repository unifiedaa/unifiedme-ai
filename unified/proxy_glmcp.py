"""Gumloop MCP proxy — forces server-side MCP mode regardless of client tools.

Routes glmcp-* model requests through Gumloop WebSocket with MCP tools
executing server-side. Client-sent tools are stripped; Gumloop's attached
MCP server handles all tool execution.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

from fastapi.responses import StreamingResponse, JSONResponse

from .gumloop.client import update_gummie_config, upload_file
from .proxy_gumloop import (
    _ensure_turnstile_key,
    _get_auth,
    _get_turnstile,
    _convert_openai_messages_simple,
    _extract_image_data,
    _ext_from_media_type,
    _rehydrate_openai_messages_if_needed,
    _stream_gumloop,
    _accumulate_gumloop,
    GUMLOOP_MODELS,
)

log = logging.getLogger("unified.proxy_glmcp")

_MCP_SYSTEM_RULES = (
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


def _map_glmcp_model(model: str) -> str:
    bare = model.removeprefix("glmcp-")
    if any(x in bare for x in ("claude", "haiku", "sonnet", "opus")):
        bare = bare.replace(".", "-")
    if bare in GUMLOOP_MODELS:
        return bare
    return bare


async def proxy_chat_completions(
    body: dict[str, Any],
    account: dict[str, Any],
    client_wants_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy chat completion to Gumloop — always in server-side MCP mode.

    Client tools are stripped. Gumloop executes tools via its attached MCP server.
    """
    from . import database as db
    from .gumloop.auth import UserDisabledError as _UserDisabledError

    auth = _get_auth(account)

    try:
        _ = await auth.get_token()
    except _UserDisabledError:
        acct_id = account.get("id", 0)
        if acct_id:
            await db.update_account(acct_id, gl_status="user_disabled",
                                    gl_error="USER_DISABLED")
        return JSONResponse(
            {"error": {"message": "Account disabled by Gumloop.", "type": "account_disabled"}},
            status_code=503,
        ), 0.0

    await _ensure_turnstile_key()
    turnstile = _get_turnstile()
    gummie_id = account.get("gl_gummie_id", "")

    if not gummie_id:
        return JSONResponse(
            {"error": {"message": "Account has no gummie_id", "type": "server_error"}},
            status_code=503,
        ), 0.0

    raw_model = body.get("model", "glmcp-claude-sonnet-4-5")
    gl_model = _map_glmcp_model(raw_model)

    body_no_tools = {k: v for k, v in body.items() if k != "tools"}
    messages, system_prompt = _convert_openai_messages_simple(body_no_tools)

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
                log.info("[GLMCP] Using existing interaction_id %s for session=%s account=%s",
                         interaction_id, chat_session_id, account_id)
        except (ValueError, TypeError) as e:
            log.warning("[GLMCP] Invalid chat_session_id '%s': %s", chat_session_id, e)

    messages, rehydration_info = await _rehydrate_openai_messages_if_needed(
        db, session_id_int if session_id_int else None, account_id, messages,
    )

    if not interaction_id and session_id_int and account_id:
        interaction_id = await db.get_or_create_gumloop_interaction_for_session_account(
            session_id_int, account_id,
        )
        log.info("[GLMCP] Created new interaction_id %s for session=%s account=%s",
                 interaction_id, chat_session_id, account_id)

    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]

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
                log.error("[GLMCP] Image upload failed: %s", e, exc_info=True)

        if gl_parts:
            msg["_gl_parts"] = gl_parts

    if not messages:
        return JSONResponse(
            {"error": {"message": "No messages provided", "type": "invalid_request_error"}},
            status_code=400,
        ), 0.0

    config_system = f"{_MCP_SYSTEM_RULES}\n{system_prompt}" if system_prompt else _MCP_SYSTEM_RULES

    try:
        await update_gummie_config(
            gummie_id=gummie_id,
            auth=auth,
            system_prompt=config_system,
            tools=None,
            model_name=gl_model,
            proxy_url=proxy_url,
        )
    except Exception as e:
        log.warning("[GLMCP] Failed to update gummie config: %s", e)

    updated = auth.get_updated_tokens()
    if updated.get("gl_id_token"):
        try:
            await db.update_account(account["id"], **updated)
        except Exception:
            pass

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

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
