"""Gumloop WebSocket chat client.

Sends messages via wss://ws.gumloop.com/ws/gummies and yields response events.
Each request needs: auth token, turnstile captcha token, gummie_id.
Supports file/image uploads via chunked upload API.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncGenerator, Optional

import httpx
import websockets
from websockets.exceptions import InvalidHandshake, InvalidMessage, InvalidStatus

from .auth import GumloopAuth
from .turnstile import TurnstileSolver

log = logging.getLogger("unified.gumloop.client")

WS_URL = "wss://ws.gumloop.com/ws/gummies"
API_BASE = "https://api.gumloop.com"
MAX_CONCURRENT = int(os.getenv("GL_MAX_CONCURRENT", "5"))
_chat_semaphore = asyncio.Semaphore(MAX_CONCURRENT)
DEFAULT_WS_OPEN_TIMEOUT = float(os.getenv("GL_WS_OPEN_TIMEOUT", "30"))
DEFAULT_WS_HANDSHAKE_RETRIES = int(os.getenv("GL_WS_HANDSHAKE_RETRIES", "3"))


async def update_gummie_config(
    gummie_id: str,
    auth: GumloopAuth,
    system_prompt: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
    proxy_url: str | None = None,
) -> dict[str, Any]:
    """Update gummie configuration via REST API before chat."""
    id_token = await auth.get_token()
    user_id = auth.user_id

    if not user_id:
        raise ValueError("User ID not available. Please login first.")

    payload: dict[str, Any] = {}
    if system_prompt is not None:
        payload["system_prompt"] = system_prompt
    if tools is not None:
        payload["tools"] = tools
    if model_name is not None:
        payload["model_name"] = model_name

    if not payload:
        return {}

    async with httpx.AsyncClient(proxy=proxy_url, timeout=30.0) as client:
        resp = await client.patch(
            f"{API_BASE}/gummies/{gummie_id}",
            json=payload,
            headers={
                "x-auth-key": user_id,
                "Authorization": f"Bearer {id_token}",
                "content-type": "application/json",
            },
        )
        resp.raise_for_status()
        return resp.json()


async def create_gummie(
    auth: GumloopAuth,
    name: str = "Proxy Agent",
    model_name: str = "claude-sonnet-4-5",
    proxy_url: str | None = None,
) -> str:
    """Create a new gummie via REST API. Returns gummie_id."""
    id_token = await auth.get_token()
    user_id = auth.user_id

    async with httpx.AsyncClient(proxy=proxy_url, timeout=60.0) as client:
        resp = await client.post(
            f"{API_BASE}/gummies",
            json={
                "name": name,
                "model_name": model_name,
                "author_id": user_id,
            },
            headers={
                "x-auth-key": user_id,
                "Authorization": f"Bearer {id_token}",
                "content-type": "application/json",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("gummie", {}).get("gummie_id", "")


async def upload_file(
    auth: GumloopAuth,
    file_data: bytes,
    file_name: str,
    content_type: str = "image/png",
    interaction_id: str = "",
    proxy_url: str | None = None,
) -> dict[str, str]:
    """Upload a file to Gumloop via chunked upload API.

    Returns {"filename": "...", "media_type": "...", "preview_url": "..."}.
    """
    id_token = await auth.get_token()
    user_id = auth.user_id

    # Build the GCS path
    ts = int(time.time() * 1000)
    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
    ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "png"
    gcs_filename = f"custom_agent_interactions/{interaction_id}/input/pasted-image-image-{ts}.{ext}"

    upload_id = str(uuid.uuid4())

    headers = {
        "x-auth-key": user_id,
        "Authorization": f"Bearer {id_token}",
    }

    # Step 1: Upload chunk (single chunk for most images)
    async with httpx.AsyncClient(proxy=proxy_url, timeout=60.0) as client:
        files = {"file": (gcs_filename, file_data, "application/octet-stream")}
        form_data = {
            "user_id": user_id,
            "chunk_index": "0",
            "total_chunks": "1",
            "file_name": gcs_filename,
        }
        resp = await client.post(
            f"{API_BASE}/upload_chunk",
            files=files,
            data=form_data,
            headers=headers,
        )
        resp.raise_for_status()
        chunk_data = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        # Use upload_id from response if available
        upload_id = chunk_data.get("upload_id", upload_id)
        log.info("[upload] Chunk uploaded: %s (%d bytes), upload_id=%s", gcs_filename, len(file_data), upload_id)

    # Step 2: Merge chunks
    async with httpx.AsyncClient(proxy=proxy_url, timeout=30.0) as client:
        merge_resp = await client.post(
            f"{API_BASE}/merge_chunks",
            json={
                "file_name": gcs_filename,
                "total_chunks": 1,
                "user_id": user_id,
                "upload_id": upload_id,
                "content_type": content_type,
                "return_preview_url": True,
            },
            headers={**headers, "content-type": "application/json"},
        )
        if merge_resp.status_code != 200:
            log.error("[upload] Merge failed: %d %s", merge_resp.status_code, merge_resp.text[:200])
        merge_resp.raise_for_status()
        merge_data = merge_resp.json()
        log.info("[upload] Merge complete: %s", gcs_filename)

    # Build preview URL (from merge response or construct it)
    preview_url = merge_data.get("preview_url", merge_data.get("url", ""))
    if not preview_url:
        preview_url = f"https://storage.googleapis.com/agenthub/uid-{user_id}/{gcs_filename}"

    return {
        "filename": gcs_filename,
        "media_type": content_type,
        "preview_url": preview_url,
    }


TURNSTILE_FAILED_ERRORS = ("turnstile_verification_failed", "captcha_verification_failed", "captcha_failed")
MAX_TURNSTILE_RETRIES = 3


async def send_chat(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: GumloopAuth,
    turnstile: TurnstileSolver | None = None,
    interaction_id: str | None = None,
    proxy_url: str | None = None,
    open_timeout: float | None = None,
    handshake_retries: int | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Send chat message via WebSocket and yield response events."""
    async with _chat_semaphore:
        for attempt in range(1, MAX_TURNSTILE_RETRIES + 1):
            got_turnstile_error = False
            async for event in _send_chat_inner(
                gummie_id, messages, auth, turnstile, interaction_id, proxy_url, open_timeout, handshake_retries
            ):
                if event.get("type") == "error":
                    err_msg = str(event.get("error", ""))
                    if any(keyword in err_msg.lower() for keyword in TURNSTILE_FAILED_ERRORS):
                        got_turnstile_error = True
                        break
                yield event
            if not got_turnstile_error:
                return
            if turnstile:
                invalidate = getattr(turnstile, "invalidate", None)
                if callable(invalidate):
                    maybe_result = invalidate()
                    if asyncio.iscoroutine(maybe_result):
                        await maybe_result
                else:
                    if hasattr(turnstile, "_ready_token"):
                        turnstile._ready_token = None
                    if hasattr(turnstile, "_ready_at"):
                        turnstile._ready_at = 0
            if attempt < MAX_TURNSTILE_RETRIES:
                yield {
                    "type": "turnstile_retry",
                    "attempt": attempt,
                    "max": MAX_TURNSTILE_RETRIES,
                    "retry_after": 2,
                    "error": "turnstile_verification_failed",
                }
                await asyncio.sleep(2)
            else:
                yield {
                    "type": "error",
                    "error": f"turnstile_verification_failed (failed {MAX_TURNSTILE_RETRIES} attempts)",
                }


async def _send_chat_inner(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: GumloopAuth,
    turnstile: TurnstileSolver | None = None,
    interaction_id: str | None = None,
    proxy_url: str | None = None,
    open_timeout: float | None = None,
    handshake_retries: int | None = None,
) -> AsyncGenerator[dict[str, Any], None]:
    id_token = await auth.get_token()

    if not interaction_id:
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]

    turnstile_token = None
    if turnstile:
        turnstile_token = await turnstile.get_token()

    # Build Gumloop message format
    gumloop_msgs = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        msg_id = msg.get("id", f"msg_{uuid.uuid4().hex[:24]}")
        parts = msg.get("_gl_parts")  # Injected by proxy for file attachments
        ts = msg.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S.000Z"))

        if role == "assistant":
            gumloop_msgs.append({
                "id": msg_id,
                "role": "assistant",
                "parts": [{"id": f"{msg_id}_part", "type": "text", "text": content}],
            })
        else:
            user_msg: dict[str, Any] = {
                "id": msg_id,
                "role": "user",
                "content": content,
                "timestamp": ts,
            }
            if parts:
                user_msg["parts"] = parts
                user_msg["creator_id"] = auth.user_id
            gumloop_msgs.append(user_msg)

    # Extract last user message for context.message
    last_user_msg = None
    for msg in reversed(messages):
        if msg.get("role", "user") == "user":
            last_user_msg = msg
            break
    if not last_user_msg:
        last_user_msg = messages[-1] if messages else {"role": "user", "content": ""}

    last_msg_id = last_user_msg.get("id", f"msg_{uuid.uuid4().hex[:24]}")
    last_ts = last_user_msg.get("timestamp", time.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    last_parts = last_user_msg.get("_gl_parts")

    context_message: dict[str, Any] = {
        "id": last_msg_id,
        "role": "user",
        "content": last_user_msg.get("content", ""),
        "timestamp": last_ts,
    }
    if last_parts:
        context_message["parts"] = last_parts
        context_message["creator_id"] = auth.user_id

    # Build payload
    payload: dict[str, Any] = {
        "type": "start",
        "payload": {
            "id_token": id_token,
            "context": {
                "gummie_id": gummie_id,
                "message": context_message,
                "chat": {"id": interaction_id, "msgs": gumloop_msgs},
                "interaction_id": interaction_id,
            },
        },
    }

    # Inject turnstile token if available
    if turnstile_token:
        payload["payload"]["turnstile_token"] = turnstile_token
        payload["payload"]["captcha_token"] = turnstile_token
        payload["payload"]["captcha_provider"] = "turnstile"

    # WebSocket connection — don't use HTTP proxy pool for WS
    # (SOCKS proxies often don't support WebSocket upgrade)
    # websockets 13+: additional_headers; websockets 12: extra_headers
    _ws_ver = int(str(getattr(websockets, "__version__", "13.0")).split(".")[0])
    _hdr_key = "additional_headers" if _ws_ver >= 13 else "extra_headers"
    ws_kwargs: dict[str, Any] = {
        _hdr_key: {"Origin": "https://www.gumloop.com"},
        "open_timeout": open_timeout if open_timeout is not None else DEFAULT_WS_OPEN_TIMEOUT,
    }
    payload_str = json.dumps(payload)
    has_captcha = "yes" if turnstile_token else "no"
    max_handshake_retries = handshake_retries if handshake_retries is not None else DEFAULT_WS_HANDSHAKE_RETRIES

    for attempt in range(1, max_handshake_retries + 1):
        try:
            async with websockets.connect(WS_URL, **ws_kwargs) as ws:
                log.info(
                    "[WS] Sending (%d bytes), captcha=%s, handshake_attempt=%s/%s, open_timeout=%s",
                    len(payload_str),
                    has_captcha,
                    attempt,
                    max_handshake_retries,
                    ws_kwargs["open_timeout"],
                )
                await ws.send(payload_str)

                async for message in ws:
                    try:
                        event = json.loads(message)
                        yield event
                        if event.get("type") == "finish" and event.get("final", True):
                            break
                    except json.JSONDecodeError:
                        continue
                return
        except (TimeoutError, InvalidHandshake, InvalidMessage, InvalidStatus) as e:
            if attempt >= max_handshake_retries:
                yield {"type": "error", "error": f"websocket opening handshake failed after {attempt} attempts: {e}"}
                return
            backoff = min(2 ** (attempt - 1), 8)
            log.warning(
                "[WS] Opening handshake failed attempt %s/%s; retrying in %ss: %s",
                attempt,
                max_handshake_retries,
                backoff,
                e,
            )
            yield {
                "type": "handshake_retry",
                "attempt": attempt,
                "max": max_handshake_retries,
                "retry_after": backoff,
                "error": str(e) or e.__class__.__name__,
            }
            await asyncio.sleep(backoff)


class GumloopStreamHandler:
    """Handle Gumloop WebSocket events and convert to normalized format."""

    def __init__(self, model: str = "claude-sonnet-4-5", input_tokens: int = 0):
        self.model = model
        self.input_tokens = input_tokens
        self.output_tokens = 0
        self.cached_tokens = 0
        self.cache_creation_input_tokens = 0
        self.total_tokens = 0
        self.text_buffer: list[str] = []
        self.reasoning_buffer: list[str] = []
        self.block_index = -1
        self.in_text = False
        self.in_reasoning = False
        self.finished = False
        self.response_ended = False

    def handle_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if self.response_ended:
            return {"type": "ignored", "reason": "response_ended"}

        event_type = event.get("type", "")

        if event_type == "step-start":
            return {"type": "step_start", "id": event.get("id")}

        elif event_type == "reasoning-start":
            self.in_reasoning = True
            self.block_index += 1
            return {"type": "reasoning_start", "index": self.block_index}

        elif event_type == "reasoning-delta":
            delta = event.get("delta", "")
            if delta:
                self.reasoning_buffer.append(delta)
            return {"type": "reasoning_delta", "delta": delta, "index": self.block_index}

        elif event_type == "reasoning-end":
            self.in_reasoning = False
            return {"type": "reasoning_end", "index": self.block_index}

        elif event_type == "text-start":
            self.in_text = True
            self.block_index += 1
            return {"type": "text_start", "index": self.block_index}

        elif event_type == "text-delta":
            delta = event.get("delta", "")
            if delta:
                self.text_buffer.append(delta)
            return {"type": "text_delta", "delta": delta, "index": self.block_index}

        elif event_type == "text-end":
            self.in_text = False
            return {"type": "text_end", "index": self.block_index}

        elif event_type == "tool-input-start":
            return {"type": "tool_input_start", "tool": event.get("toolName", "")}

        elif event_type == "tool-input-delta":
            return {"type": "tool_input_delta", "delta": event.get("delta", "")}

        elif event_type == "tool-call":
            return {"type": "tool_call", "tool": event.get("toolName", "")}

        elif event_type == "tool-result":
            # Tool result contains the actual output text
            result_text = event.get("result", "")
            if isinstance(result_text, dict):
                result_text = result_text.get("output", result_text.get("text", str(result_text)))
            if result_text:
                self.text_buffer.append(str(result_text))
            return {"type": "tool_result", "tool": event.get("toolName", ""), "result": result_text}

        elif event_type == "finish":
            is_final = event.get("final", True)
            if is_final:
                self.finished = True
                self.response_ended = True
            usage = event.get("usage", {})
            self.output_tokens = usage.get(
                "output_tokens", len("".join(self.text_buffer)) // 4
            )
            self.input_tokens = usage.get("input_tokens", self.input_tokens)
            self.cached_tokens = usage.get("cached_tokens", 0)
            self.cache_creation_input_tokens = usage.get(
                "cache_creation_input_tokens", 0
            )
            self.total_tokens = usage.get(
                "total_tokens", self.input_tokens + self.output_tokens
            )
            return {
                "type": "finish",
                "final": is_final,
                "finish_reason": event.get("finishReason", "end_turn"),
                "usage": {
                    "input_tokens": self.input_tokens,
                    "output_tokens": self.output_tokens,
                    "total_tokens": self.total_tokens,
                    "cache_creation_input_tokens": self.cache_creation_input_tokens,
                    "cache_read_input_tokens": self.cached_tokens,
                },
            }

        return {"type": "unknown", "raw": event}

    def get_full_text(self) -> str:
        return "".join(self.text_buffer)

    def get_full_reasoning(self) -> str:
        return "".join(self.reasoning_buffer)
