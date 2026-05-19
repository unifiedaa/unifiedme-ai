"""FastAPI router for /api/chat/* — Chat session management + streaming proxy."""

from __future__ import annotations

import json
import logging

import httpx
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse

from .auth_middleware import verify_admin
from .config import LISTEN_PORT
from . import database as db

log = logging.getLogger("unified.chat")

router = APIRouter(prefix="/api/chat", tags=["chat"])


# ─── Sessions ────────────────────────────────────────────────────────────────


@router.get("/sessions")
async def list_sessions(_: bool = Depends(verify_admin)):
    """List all chat sessions."""
    sessions = await db.get_chat_sessions()
    return {"sessions": sessions}


@router.post("/sessions")
async def create_session(request: Request, _: bool = Depends(verify_admin)):
    """Create a new chat session. Body: {title?, model?}."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    title = str(body.get("title", "New Chat")).strip()
    model = str(body.get("model", "")).strip()
    sid = await db.create_chat_session(title, model)
    return {"ok": True, "id": sid}


@router.get("/sessions/{session_id}")
async def get_session(session_id: int, _: bool = Depends(verify_admin)):
    """Get a session with all messages."""
    session = await db.get_chat_session(session_id)
    if not session:
        return JSONResponse({"error": "Not found"}, status_code=404)
    messages = await db.get_chat_messages(session_id)
    return {"session": session, "messages": messages}


@router.put("/sessions/{session_id}")
async def update_session(session_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Update session title/model."""
    body = await request.json()
    fields = {}
    if "title" in body:
        fields["title"] = str(body["title"]).strip()
    if "model" in body:
        fields["model"] = str(body["model"]).strip()
    if not fields:
        return JSONResponse({"error": "No fields"}, status_code=400)
    await db.update_chat_session(session_id, **fields)
    return {"ok": True}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: int, _: bool = Depends(verify_admin)):
    """Delete a chat session and all its messages."""
    await db.delete_chat_session(session_id)
    return {"ok": True}


@router.delete("/sessions")
async def delete_all_sessions(_: bool = Depends(verify_admin)):
    """Delete ALL chat sessions."""
    count = await db.delete_all_chat_sessions()
    return {"ok": True, "deleted": count}


# ─── Messages ────────────────────────────────────────────────────────────────


@router.post("/sessions/{session_id}/messages")
async def add_message(session_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Add a message to a session. Body: {role, content, model?}."""
    body = await request.json()
    role = str(body.get("role", "user")).strip()
    content = str(body.get("content", "")).strip()
    model = str(body.get("model", "")).strip()
    if not content:
        return JSONResponse({"error": "content required"}, status_code=400)
    mid = await db.add_chat_message(session_id, role, content, model)
    return {"ok": True, "id": mid}


@router.post("/opencode-sync")
async def opencode_sync(request: Request):
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        return JSONResponse({"error": "local only"}, status_code=403)

    body = await request.json()
    opencode_session_id = str(body.get("session_id", "")).strip()
    opencode_message_id = str(body.get("message_id", "")).strip()
    role = str(body.get("role", "")).strip()
    content = str(body.get("content", "")).strip()
    model = str(body.get("model", "")).strip()

    if not opencode_session_id or role not in {"user", "assistant"} or not content:
        return {"ok": True, "skipped": True}

    chat_session_id = await db.get_or_create_chat_session_for_opencode_session(
        opencode_session_id,
        title=f"OpenCode {opencode_session_id}",
        model=model,
    )

    existing = await db.get_chat_message_by_opencode_message_id(chat_session_id, opencode_message_id)
    if existing:
        return {"ok": True, "session_id": chat_session_id, "deduped": True}

    prev = await db.get_last_chat_message(chat_session_id)
    if not opencode_message_id and prev and prev.get("role") == role and prev.get("content") == content:
        return {"ok": True, "session_id": chat_session_id, "deduped": True}

    mid = await db.add_chat_message(chat_session_id, role, content, model, opencode_message_id=opencode_message_id)
    return {"ok": True, "session_id": chat_session_id, "message_id": mid}


@router.post("/opencode-sync/delete-session")
async def opencode_sync_delete_session(request: Request):
    client_host = (request.client.host if request.client else "") or ""
    if client_host not in {"127.0.0.1", "::1", "localhost"}:
        return JSONResponse({"error": "local only"}, status_code=403)

    body = await request.json()
    opencode_session_id = str(body.get("session_id", "")).strip()
    if not opencode_session_id:
        return {"ok": True, "skipped": True}

    session = await db.get_chat_session_by_opencode_session_key(opencode_session_id)
    if not session:
        return {"ok": True, "deleted": False}

    deleted = await db.delete_chat_session(int(session["id"]))
    return {"ok": True, "deleted": bool(deleted)}


# ─── Export / Import ─────────────────────────────────────────────────────────


@router.get("/export")
async def export_chats(_: bool = Depends(verify_admin)):
    """Export all chat data as JSON."""
    data = await db.export_chat_data()
    return data


@router.post("/import")
async def import_chats(request: Request, _: bool = Depends(verify_admin)):
    """Import chat data from JSON. Body: {sessions: [...]}."""
    body = await request.json()
    count = await db.import_chat_data(body)
    return {"ok": True, "imported": count}


# ─── Streaming Chat Completion ───────────────────────────────────────────────


@router.post("/completions")
async def chat_completions(request: Request, _: bool = Depends(verify_admin)):
    """Proxy chat completion to local /v1/chat/completions with streaming.

    Body: {session_id, model, messages: [{role, content}], stream?: true}
    Saves user message + assistant response to DB.
    Returns SSE stream.
    """
    body = await request.json()
    session_id = body.get("session_id")
    model = str(body.get("model", "claude-sonnet-4")).strip()
    messages = body.get("messages", [])
    stream = body.get("stream", True)

    if not messages:
        return JSONResponse({"error": "messages required"}, status_code=400)

    # Get API key from DB
    keys = await db.get_api_keys()
    if not keys:
        return JSONResponse({"error": "No API keys available"}, status_code=500)
    # Generate a temp key for this request
    _, api_key = await db.create_api_key("_chat_temp")

    # Save user message to DB
    if session_id:
        last_msg = messages[-1] if messages else {}
        if last_msg.get("role") == "user":
            await db.add_chat_message(session_id, "user", last_msg["content"])
            # Auto-title from first message
            session = await db.get_chat_session(session_id)
            if session and session.get("title") == "New Chat":
                title = last_msg["content"][:50].strip()
                if title:
                    await db.update_chat_session(session_id, title=title)

    # Proxy to local endpoint
    proxy_url = f"http://127.0.0.1:{LISTEN_PORT}/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if session_id:
        payload["chat_session_id"] = session_id

    if not stream:
        # Non-streaming
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                resp = await client.post(proxy_url, json=payload, headers=headers)
                data = resp.json()
                # Save assistant response
                if session_id and resp.status_code == 200:
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                    if content:
                        await db.add_chat_message(session_id, "assistant", content, model)
                # Cleanup temp key
                await db.revoke_api_key((await db.get_api_keys())[-1]["id"])
                return data
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    # Streaming response
    async def stream_response():
        full_content = ""
        thinking_content = ""
        try:
            async with httpx.AsyncClient(timeout=300) as client:
                async with client.stream("POST", proxy_url, json=payload, headers=headers) as resp:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        yield f"{line}\n\n"
                        # Parse SSE to collect full response
                        if line.startswith("data: ") and line != "data: [DONE]":
                            try:
                                chunk = json.loads(line[6:])
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                if delta.get("content"):
                                    full_content += delta["content"]
                                # Thinking mode (some models return thinking in a separate field)
                                if delta.get("thinking"):
                                    thinking_content += delta["thinking"]
                            except (json.JSONDecodeError, IndexError, KeyError):
                                pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # Save to DB after stream completes
        if session_id and full_content:
            await db.add_chat_message(session_id, "assistant", full_content, model)
        if session_id and thinking_content:
            await db.add_chat_message(session_id, "thinking", thinking_content, model)

        # Cleanup temp key
        try:
            all_keys = await db.get_api_keys()
            for k in all_keys:
                if k.get("name") == "_chat_temp":
                    await db.revoke_api_key(k["id"])
        except Exception:
            pass

    return StreamingResponse(
        stream_response(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"},
    )


# ─── Agent Mode (MCP Tool Execution) ────────────────────────────────────────


@router.post("/agent-completions")
async def agent_completions(request: Request, _: bool = Depends(verify_admin)):
    """Agent mode: LLM + MCP tool execution loop with streaming.

    Body: {session_id, model, messages, mcp_server_url}
    Connects to MCP server, fetches tools, runs agent loop.
    Streams SSE events (content, thinking, tool calls, tool results).
    """
    body = await request.json()
    session_id = body.get("session_id")
    model = str(body.get("model", "claude-sonnet-4")).strip()
    messages = body.get("messages", [])
    mcp_server_url = str(body.get("mcp_server_url", "")).strip()

    if not messages:
        return JSONResponse({"error": "messages required"}, status_code=400)
    if not mcp_server_url:
        return JSONResponse({"error": "mcp_server_url required"}, status_code=400)

    # Get API key from DB
    keys = await db.get_api_keys()
    if not keys:
        return JSONResponse({"error": "No API keys available"}, status_code=500)
    _, api_key = await db.create_api_key("_chat_temp")

    # Save user message to DB
    if session_id:
        last_msg = messages[-1] if messages else {}
        if last_msg.get("role") == "user":
            await db.add_chat_message(session_id, "user", last_msg["content"])
            session = await db.get_chat_session(session_id)
            if session and session.get("title") == "New Chat":
                title = last_msg["content"][:50].strip()
                if title:
                    await db.update_chat_session(session_id, title=title)

    from .agent_loop import run_agent_loop

    async def stream_agent():
        full_content = ""
        try:
            async for sse_event in run_agent_loop(api_key, model, messages, mcp_server_url):
                yield sse_event
                # Parse to collect full content for DB save
                if sse_event.startswith("data: ") and sse_event.strip() != "data: [DONE]":
                    try:
                        chunk = json.loads(sse_event[6:].strip())
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            full_content += delta["content"]
                    except (json.JSONDecodeError, IndexError, KeyError):
                        pass
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"

        # Save full response to DB
        if session_id and full_content:
            await db.add_chat_message(session_id, "assistant", full_content, model)

        # Cleanup temp key
        try:
            all_keys = await db.get_api_keys()
            for k in all_keys:
                if k.get("name") == "_chat_temp":
                    await db.revoke_api_key(k["id"])
        except Exception:
            pass

    return StreamingResponse(
        stream_agent(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache, no-store", "Connection": "keep-alive", "X-Accel-Buffering": "no", "X-Content-Type-Options": "nosniff"},
    )


# ─── MCP Server Ping ────────────────────────────────────────────────────────


@router.post("/mcp-ping")
async def mcp_ping(request: Request, _: bool = Depends(verify_admin)):
    """Test MCP server connectivity. Body: {mcp_server_url}."""
    body = await request.json()
    mcp_url = str(body.get("mcp_server_url", "")).strip()
    if not mcp_url:
        return JSONResponse({"error": "mcp_server_url required"}, status_code=400)

    from .mcp_client import MCPClient

    client = MCPClient(mcp_url, timeout=10.0)
    try:
        await client.connect()
        tools = await client.list_tools()
        tool_names = [t.get("name", "?") for t in tools]
        return {"ok": True, "tools": len(tools), "tool_names": tool_names}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=502)
    finally:
        await client.disconnect()


# ─── Models List (for chat UI) ──────────────────────────────────────────────


@router.get("/models")
async def list_models(_: bool = Depends(verify_admin)):
    """Return available models for the chat UI."""
    from .config import MODEL_TIER, _HIDDEN_ALIASES, Tier
    models = []
    for name, tier in MODEL_TIER.items():
        if name in _HIDDEN_ALIASES:
            continue
        models.append({"id": name, "tier": tier.value})
    return {"models": models}
