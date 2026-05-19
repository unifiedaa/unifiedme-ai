"""TheRouter proxy — OpenAI-compatible passthrough to api.therouter.ai.

TheRouter's API at https://api.therouter.ai/v1 is OpenAI-compatible.
We passthrough requests, swapping the auth header with the account's API key.
Model name: strip "tr-" prefix before sending upstream.
Variants (:online, :thinking, :extended, :exacto) are passed through as-is
since TheRouter uses the provider/model:variant format natively.
"""

from __future__ import annotations

import json
import logging
from typing import AsyncIterator

import httpx
from fastapi.responses import StreamingResponse, JSONResponse

from .config import THEROUTER_UPSTREAM

log = logging.getLogger("unified.proxy_therouter")

THEROUTER_CHAT_URL = f"{THEROUTER_UPSTREAM}/v1/chat/completions"

# Client pool keyed by proxy URL
_clients: dict[str, httpx.AsyncClient] = {}


async def get_client(proxy_url: str | None = None) -> httpx.AsyncClient:
    key = proxy_url or "__direct__"
    if key not in _clients:
        _clients[key] = httpx.AsyncClient(
            timeout=httpx.Timeout(connect=30, read=300, write=30, pool=10),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
            follow_redirects=True,
            proxy=proxy_url,
        )
    return _clients[key]


async def close_all_clients() -> None:
    global _clients
    for c in _clients.values():
        await c.aclose()
    _clients.clear()


def _resolve_upstream_model(model: str) -> str:
    """Convert tr-* model name to TheRouter upstream format.

    tr-claude-opus-4.6          -> anthropic/claude-opus-4.6
    tr-gpt-5.4:online           -> openai/gpt-5.4:online
    tr-gpt-5.3-codex:thinking   -> openai/gpt-5.3-codex:thinking

    TheRouter uses provider/model format. Provider is inferred from model name.
    """
    if model.startswith("tr-"):
        model = model[3:]  # strip "tr-"
    # Already has provider prefix
    if "/" in model:
        return model
    # Infer provider from model name
    base = model.split(":")[0]  # strip variant suffix
    if base.startswith("claude"):
        return f"anthropic/{model}"
    if base.startswith("gemini"):
        return f"google/{model}"
    if base.startswith("deepseek"):
        return f"deepseek/{model}"
    # Default: openai (gpt-*, o3-*, o4-*, etc.)
    return f"openai/{model}"


async def proxy_chat_completions(
    body: dict,
    api_key: str,
    is_stream: bool,
    proxy_url: str | None = None,
) -> tuple[StreamingResponse | JSONResponse, float]:
    """Proxy a chat completion request to TheRouter.

    Returns (response, cost). Cost is 0 for now (free tier).
    """
    client = await get_client(proxy_url)

    # Resolve model name
    original_model = body.get("model", "")
    upstream_model = _resolve_upstream_model(original_model)
    body = {**body, "model": upstream_model, "stream": is_stream}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if is_stream:
        headers["Accept"] = "text/event-stream"

    try:
        req = client.build_request(
            method="POST",
            url=THEROUTER_CHAT_URL,
            json=body,
            headers=headers,
        )
        upstream_resp = await client.send(req, stream=is_stream)
    except httpx.ConnectError as e:
        log.error("TheRouter connect error: %s", e)
        return JSONResponse(
            {"error": {"message": f"TheRouter connection error: {e}", "type": "server_error"}},
            status_code=502,
        ), 0.0
    except httpx.TimeoutException as e:
        log.error("TheRouter timeout: %s", e)
        return JSONResponse(
            {"error": {"message": f"TheRouter timeout: {e}", "type": "server_error"}},
            status_code=504,
        ), 0.0

    status = upstream_resp.status_code

    if status >= 400:
        error_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        try:
            error_json = json.loads(error_body)
        except (json.JSONDecodeError, ValueError):
            error_json = {"message": error_body.decode(errors="replace")[:1000]}
        log.warning("TheRouter HTTP %d: %s", status, json.dumps(error_json)[:300])
        return JSONResponse(
            {"error": error_json},
            status_code=status,
        ), 0.0

    if not is_stream:
        # Non-streaming: collect full response
        full_body = await upstream_resp.aread()
        await upstream_resp.aclose()
        try:
            data = json.loads(full_body)
            # Restore original model name in response
            data["model"] = original_model
            return JSONResponse(data, status_code=200), 0.0
        except (json.JSONDecodeError, ValueError):
            return JSONResponse(
                {"error": {"message": "Invalid response from TheRouter", "type": "server_error"}},
                status_code=502,
            ), 0.0

    # Streaming: passthrough SSE with model name restoration
    _stream_state = {"content": "", "done": False}

    async def stream_generator() -> AsyncIterator[bytes]:
        try:
            async for line in upstream_resp.aiter_lines():
                if not line:
                    yield b"\n"
                    continue
                if line.startswith("data: "):
                    data_str = line[6:]
                    if data_str == "[DONE]":
                        yield b"data: [DONE]\n\n"
                        break
                    try:
                        chunk = json.loads(data_str)
                        chunk["model"] = original_model
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        if delta.get("content"):
                            _stream_state["content"] += delta["content"]
                        yield f"data: {json.dumps(chunk)}\n\n".encode()
                    except (json.JSONDecodeError, ValueError):
                        yield f"{line}\n".encode()
                else:
                    yield f"{line}\n".encode()
        except Exception as e:
            log.error("TheRouter stream error: %s", e)
        finally:
            await upstream_resp.aclose()
            _stream_state["done"] = True

    resp = StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
    resp._ws_stream_state = _stream_state  # pyright: ignore[reportAttributeAccessIssue]
    return resp, 0.0
