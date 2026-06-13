"""FastAPI router for /api/* admin endpoints."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path as _Path
from typing import Optional

import httpx
from fastapi import APIRouter, Request, Depends
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .auth_middleware import verify_admin
from .models import AccountCreate, ApiKeyCreate, BatchLoginRequest
from .account_manager import (
    refresh_credits, auto_trash, refresh_all_credits as _refresh_all_credits,
    refresh_kiro_credits, refresh_cb_credits, check_account_health,
)
from .batch_runner import batch_state, start_batch, cancel_batch, retry_account
from .config import MODEL_TIER, Tier, LISTEN_PORT, WAVESPEED_MODELS, MAX_GL_MODELS, ALL_MODELS, _HIDDEN_ALIASES
from . import database as db

log = logging.getLogger("unified.router_admin")

router = APIRouter(prefix="/api", tags=["admin"])


class TestModelRequest(BaseModel):
    model: str = "claude-sonnet-4.5"


# ---------------------------------------------------------------------------
# Models endpoint (admin, no API key needed)
# ---------------------------------------------------------------------------

@router.get("/models")
async def admin_list_models(_: bool = Depends(verify_admin)):
    """Return combined model list (admin endpoint, no API key needed)."""
    models = []
    for model_name, tier in MODEL_TIER.items():
        if model_name in _HIDDEN_ALIASES:
            continue
        models.append({
            "id": model_name,
            "object": "model",
            "created": 1700000000,
            "owned_by": (
                "kiro" if tier == Tier.STANDARD else
                "wavespeed" if tier == Tier.WAVESPEED else
                "gumloop" if tier == Tier.MAX_GL else
                "chatbai" if tier == Tier.CHATBAI else
                "skillboss" if tier == Tier.SKILLBOSS else
                "windsurf" if tier == Tier.WINDSURF else
                "therouter" if tier == Tier.THEROUTER else
                "codebuddy"
            ),
            "tier": tier.value,
        })
    return {"object": "list", "data": models}


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------

@router.get("/accounts")
async def list_accounts(request: Request, _: bool = Depends(verify_admin)):
    """List all accounts grouped by status."""
    all_accounts = await db.get_accounts()

    grouped: dict[str, list] = {"active": [], "failed": [], "trash": [], "banned": []}
    for acc in all_accounts:
        info = {
            "id": acc["id"],
            "email": acc["email"],
            "status": acc["status"],
            "kiro_status": acc["kiro_status"],
            "cb_status": acc["cb_status"],
            "kiro_credits": acc.get("kiro_credits", 0),
            "kiro_credits_total": acc.get("kiro_credits_total", 0),
            "kiro_credits_used": acc.get("kiro_credits_used", 0),
            "cb_credits": acc.get("cb_credits", 0),
            "kiro_error": acc["kiro_error"],
            "cb_error": acc["cb_error"],
            "kiro_error_count": acc["kiro_error_count"],
            "cb_error_count": acc["cb_error_count"],
            "last_used_kiro": acc["last_used_kiro"],
            "last_used_cb": acc["last_used_cb"],
            "created_at": acc["created_at"],
            "cb_expires_at": acc.get("cb_expires_at", ""),
            "kiro_expires_at": acc.get("kiro_expires_at", ""),
            "ws_status": acc.get("ws_status", "none"),
            "ws_api_key": acc.get("ws_api_key", ""),
            "ws_credits": acc.get("ws_credits", 0),
            "ws_error": acc.get("ws_error", ""),
            "last_used_ws": acc.get("last_used_ws", ""),
            "kiro_verified": acc.get("kiro_verified", 0),
            "cb_verified": acc.get("cb_verified", 0),
            "ws_verified": acc.get("ws_verified", 0),
            "kiro_test_error": acc.get("kiro_test_error", ""),
            "cb_test_error": acc.get("cb_test_error", ""),
            "ws_test_error": acc.get("ws_test_error", ""),
            "gl_status": acc.get("gl_status", "none"),
            "gl_gummie_id": acc.get("gl_gummie_id", ""),
            "gl_credits": acc.get("gl_credits", 0),
            "gl_error": acc.get("gl_error", ""),
            "gl_error_count": acc.get("gl_error_count", 0),
            "last_used_gl": acc.get("last_used_gl", ""),
            "gl_verified": acc.get("gl_verified", 0),
            "gl_test_error": acc.get("gl_test_error", ""),
            "cbai_status": acc.get("cbai_status", "none"),
            "cbai_api_key": acc.get("cbai_api_key", ""),
            "cbai_credits": acc.get("cbai_credits", 0),
            "cbai_error": acc.get("cbai_error", ""),
            "cbai_error_count": acc.get("cbai_error_count", 0),
            "last_used_cbai": acc.get("last_used_cbai", ""),
            "cbai_verified": acc.get("cbai_verified", 0),
            "cbai_test_error": acc.get("cbai_test_error", ""),
            "skboss_status": acc.get("skboss_status", "none"),
            "skboss_api_key": acc.get("skboss_api_key", ""),
            "skboss_credits": acc.get("skboss_credits", 0),
            "skboss_error": acc.get("skboss_error", ""),
            "skboss_error_count": acc.get("skboss_error_count", 0),
            "last_used_skboss": acc.get("last_used_skboss", ""),
            "skboss_verified": acc.get("skboss_verified", 0),
            "skboss_test_error": acc.get("skboss_test_error", ""),
            "windsurf_status": acc.get("windsurf_status", "none"),
            "windsurf_api_key": acc.get("windsurf_api_key", ""),
            "windsurf_credits": acc.get("windsurf_credits", 0),
            "windsurf_error": acc.get("windsurf_error", ""),
            "windsurf_error_count": acc.get("windsurf_error_count", 0),
            "last_used_windsurf": acc.get("last_used_windsurf", ""),
            "tr_status": acc.get("tr_status", "none"),
            "tr_api_key": acc.get("tr_api_key", ""),
            "tr_credits": acc.get("tr_credits", 0),
            "tr_error": acc.get("tr_error", ""),
            "tr_error_count": acc.get("tr_error_count", 0),
            "last_used_tr": acc.get("last_used_tr", ""),
            "tr_verified": acc.get("tr_verified", 0),
            "tr_test_error": acc.get("tr_test_error", ""),
        }
        bucket = acc["status"] if acc["status"] in grouped else "active"
        grouped[bucket].append(info)

    sticky = {
        "standard": await db.get_sticky_account_id("standard"),
        "max": await db.get_sticky_account_id("max"),
        "wavespeed": await db.get_sticky_account_id("wavespeed"),
        "max_gl": await db.get_sticky_account_id("max_gl"),
        "chatbai": await db.get_sticky_account_id("chatbai"),
        "skillboss": await db.get_sticky_account_id("skillboss"),
        "windsurf": await db.get_sticky_account_id("windsurf"),
        "therouter": await db.get_sticky_account_id("therouter"),
    }

    return {
        "accounts": grouped,
        "total": len(all_accounts),
        "active": len(grouped["active"]),
        "failed": len(grouped["failed"]),
        "trash": len(grouped["trash"]),
        "sticky": sticky,
    }


@router.post("/accounts/add")
async def add_accounts(req: AccountCreate, request: Request, _: bool = Depends(verify_admin)):
    """Add accounts from email:password lines. Pushes to D1 immediately for cross-device sync."""
    added = 0
    skipped = 0
    errors: list[str] = []
    new_account_ids: list[int] = []

    for line in req.accounts:
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        email = parts[0].strip()
        password = parts[1].strip()
        if not email or not password:
            continue

        existing = await db.get_account_by_email(email)
        if existing:
            skipped += 1
            continue

        try:
            account_id = await db.create_account(email, password)
            new_account_ids.append(account_id)
            added += 1
        except Exception as e:
            errors.append(f"{email}: {e}")

    # Push new accounts to D1 immediately so other devices can see them
    if new_account_ids:
        try:
            from . import license_client
            for account_id in new_account_ids:
                await license_client.d1_sync_account(account_id)
        except Exception:
            pass  # Will sync on next heartbeat

    return {"added": added, "skipped": skipped, "errors": errors}


@router.post("/accounts/import-gumloop")
async def import_gumloop_account(request: Request, _: bool = Depends(verify_admin)):
    """Import a Gumloop account with pre-obtained tokens from browser signup.

    Body: {
        email: str,              # Account email
        refresh_token: str,      # Firebase refresh token (from browser signup)
        user_id: str,            # Firebase user ID
        gummie_id: str,          # Gumloop gummie ID
        password?: str,          # Optional password (for DB record)
    }
    """
    body = await request.json()
    email = str(body.get("email", "")).strip()
    refresh_token = str(body.get("refresh_token", "")).strip()
    user_id = str(body.get("user_id", "")).strip()
    gummie_id = str(body.get("gummie_id", "")).strip()
    password = str(body.get("password", "gumloop")).strip()

    if not email or not refresh_token or not user_id:
        return JSONResponse(
            {"error": "email, refresh_token, and user_id are required"},
            status_code=400,
        )

    # Find or create account
    existing = await db.get_account_by_email(email)
    if existing:
        account_id = existing["id"]
    else:
        account_id = await db.create_account(email, password)

    # Verify token works by refreshing
    from .gumloop.auth import GumloopAuth
    auth = GumloopAuth(refresh_token=refresh_token, user_id=user_id)
    try:
        id_token = await auth.get_token()
        updated = auth.get_updated_tokens()
    except Exception as e:
        return JSONResponse(
            {"error": f"Token refresh failed: {e}"},
            status_code=400,
        )

    # Update account with GL data
    await db.update_account(
        account_id,
        gl_status="ok",
        gl_refresh_token=updated.get("gl_refresh_token", refresh_token),
        gl_user_id=updated.get("gl_user_id", user_id),
        gl_gummie_id=gummie_id,
        gl_id_token=updated.get("gl_id_token", ""),
        gl_error="",
        gl_error_count=0,
    )

    return {
        "ok": True,
        "account_id": account_id,
        "email": email,
        "gl_status": "ok",
        "gummie_id": gummie_id,
        "message": "Gumloop account imported and token verified",
    }


@router.post("/accounts/{account_id}/sticky/{tier}")
async def set_sticky_account_endpoint(account_id: int, tier: str, _: bool = Depends(verify_admin)):
    """Set an account as the sticky (pinned) account for a tier.

    Pinned accounts won't be auto-cleared on errors — they stay selected
    until manually cleared by the user.
    """
    valid_tiers = {"standard", "max", "wavespeed", "max_gl", "chatbai", "skillboss", "windsurf", "therouter"}
    if tier not in valid_tiers:
        return JSONResponse({"error": f"Invalid tier: {tier}"}, status_code=400)
    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    await db.set_sticky_account(tier, account_id, pinned=True)
    return {"ok": True, "tier": tier, "account_id": account_id, "pinned": True}


@router.delete("/accounts/{account_id}/sticky/{tier}")
async def clear_sticky_account_endpoint(account_id: int, tier: str, _: bool = Depends(verify_admin)):
    """Clear sticky account for a tier (force-clear, even if pinned)."""
    await db.force_clear_sticky_account(tier)
    return {"ok": True, "tier": tier}


@router.get("/accounts/{account_id}/mcp-list")
async def mcp_list_endpoint(account_id: int, _: bool = Depends(verify_admin)):
    """List MCP servers bound to a Gumloop account.

    Returns both registered secrets and which ones are active on the gummie.
    """
    import httpx

    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)

    refresh_tok = account.get("gl_refresh_token", "")
    gummie_id = account.get("gl_gummie_id", "")
    if not refresh_tok:
        return JSONResponse({"error": "Account has no Gumloop credentials"}, status_code=400)

    # Auth
    from .gumloop.auth import GumloopAuth
    proxy_info = await db.get_proxy_for_batch()
    proxy_url = proxy_info["url"] if proxy_info else None

    auth = GumloopAuth(
        refresh_token=refresh_tok,
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
        proxy_url=proxy_url,
    )
    try:
        id_token = await auth.get_token()
    except Exception as e:
        return JSONResponse({"error": f"Token refresh failed: {e}"}, status_code=500)

    user_id = auth.user_id
    headers = {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
    }

    client_kwargs = {"timeout": 30}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Get registered MCP secrets
        resp = await client.get("https://api.gumloop.com//secrets/mcp_servers", headers=headers)
        secrets = resp.json() if resp.status_code == 200 else []

        # Get gummie tools to see which MCPs are active
        active_mcp_urls = []
        if gummie_id:
            resp2 = await client.get(f"https://api.gumloop.com/gummies/{gummie_id}", headers=headers)
            if resp2.status_code == 200:
                tools = resp2.json().get("gummie", {}).get("tools", [])
                active_mcp_urls = [t.get("mcp_server_url", "") for t in tools if t.get("type") == "mcp_server"]

    # Build result
    mcp_list = []
    for s in secrets:
        url = s.get("url", "")
        mcp_list.append({
            "secret_id": s.get("secret_id", ""),
            "name": s.get("nickname", ""),
            "url": url,
            "active": url in active_mcp_urls,
        })

    # Persist refreshed tokens
    updated = auth.get_updated_tokens()
    if updated.get("gl_id_token"):
        try:
            await db.update_account(account_id, **updated)
        except Exception:
            pass

    return {"ok": True, "mcp_servers": mcp_list, "email": account.get("email", "")}


@router.post("/accounts/{account_id}/mcp-toggle")
async def mcp_toggle_endpoint(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Toggle MCP server(s) on/off for a Gumloop account's gummie.

    Body: {"enable": ["url1", "url2"], "disable": ["url3"]}
    Rebuilds the gummie tools array with only enabled MCPs + built-in tools.
    """
    import httpx

    body = await request.json()
    enable_urls = set(body.get("enable", []))
    disable_urls = set(body.get("disable", []))

    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)

    gummie_id = account.get("gl_gummie_id", "")
    refresh_tok = account.get("gl_refresh_token", "")
    if not gummie_id or not refresh_tok:
        return JSONResponse({"error": "Account has no gummie or credentials"}, status_code=400)

    from .gumloop.auth import GumloopAuth
    proxy_info = await db.get_proxy_for_batch()
    proxy_url = proxy_info["url"] if proxy_info else None

    auth = GumloopAuth(
        refresh_token=refresh_tok,
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
        proxy_url=proxy_url,
    )
    try:
        id_token = await auth.get_token()
    except Exception as e:
        return JSONResponse({"error": f"Token refresh failed: {e}"}, status_code=500)

    user_id = auth.user_id
    headers = {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
    }

    client_kwargs = {"timeout": 30}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Get all registered MCP secrets
        resp = await client.get("https://api.gumloop.com//secrets/mcp_servers", headers=headers)
        secrets = resp.json() if resp.status_code == 200 else []

        # Get current gummie tools
        resp2 = await client.get(f"https://api.gumloop.com/gummies/{gummie_id}", headers=headers)
        current_tools = []
        if resp2.status_code == 200:
            current_tools = resp2.json().get("gummie", {}).get("tools", [])

        # Current active MCP URLs
        current_mcp_urls = {t.get("mcp_server_url", "") for t in current_tools if t.get("type") == "mcp_server"}

        # Calculate new active set
        new_active = set(current_mcp_urls)
        new_active |= enable_urls
        new_active -= disable_urls

        # Build new tools array
        new_tools = []
        for s in secrets:
            url = s.get("url", "")
            if url in new_active:
                new_tools.append({
                    "secret_id": s.get("secret_id", ""),
                    "mcp_server_url": url,
                    "name": s.get("nickname", ""),
                    "type": "mcp_server",
                    "restricted_tools": [],
                })

        # Add built-in tools
        new_tools.extend([
            {"metadata": {}, "type": "web_search"},
            {"metadata": {}, "type": "web_fetch"},
            {"metadata": {"model": "gemini-3.1-flash-image-preview"}, "type": "image_generator"},
            {"type": "interaction_search"},
        ])

        # Patch gummie
        resp3 = await client.patch(
            f"https://api.gumloop.com/gummies/{gummie_id}",
            json={"tools": new_tools}, headers=headers,
        )
        if resp3.status_code != 200:
            return JSONResponse({"error": f"Failed to update gummie: HTTP {resp3.status_code}"}, status_code=500)

        result_tools = resp3.json().get("gummie", {}).get("tools", [])
        mcp_count = sum(1 for t in result_tools if t.get("type") == "mcp_server")

    # Persist refreshed tokens
    updated = auth.get_updated_tokens()
    if updated.get("gl_id_token"):
        try:
            await db.update_account(account_id, **updated)
        except Exception:
            pass

    return {"ok": True, "active_mcp": mcp_count}


@router.post("/accounts/{account_id}/mcp-delete")
async def mcp_delete_endpoint(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Delete MCP server(s) from a Gumloop account by URL.

    Body: {"urls": ["url1", "url2"]}
    Removes from both gummie tools AND registered secrets.
    """
    import httpx

    body = await request.json()
    delete_urls = set(body.get("urls", []))
    if not delete_urls:
        return JSONResponse({"error": "urls list is required"}, status_code=400)

    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)

    gummie_id = account.get("gl_gummie_id", "")
    refresh_tok = account.get("gl_refresh_token", "")
    if not refresh_tok:
        return JSONResponse({"error": "Account has no Gumloop credentials"}, status_code=400)

    from .gumloop.auth import GumloopAuth
    proxy_info = await db.get_proxy_for_batch()
    proxy_url = proxy_info["url"] if proxy_info else None

    auth = GumloopAuth(
        refresh_token=refresh_tok,
        user_id=account.get("gl_user_id", ""),
        id_token=account.get("gl_id_token", ""),
        proxy_url=proxy_url,
    )
    try:
        id_token = await auth.get_token()
    except Exception as e:
        return JSONResponse({"error": f"Token refresh failed: {e}"}, status_code=500)

    user_id = auth.user_id
    headers = {
        "Authorization": f"Bearer {id_token}",
        "x-auth-key": user_id,
        "Content-Type": "application/json",
    }

    client_kwargs = {"timeout": 30}
    if proxy_url:
        client_kwargs["proxy"] = proxy_url

    deleted_secrets = 0
    errors = []

    # Normalize URLs for matching — strip trailing /mcp, /, etc.
    def _norm(u):
        u = u.rstrip("/")
        if u.endswith("/mcp"):
            u = u[:-4]
        return u.rstrip("/")

    delete_normalized = {_norm(u) for u in delete_urls}

    async with httpx.AsyncClient(**client_kwargs) as client:
        # Get registered MCP secrets
        resp = await client.get("https://api.gumloop.com//secrets/mcp_servers", headers=headers)
        secrets = resp.json() if resp.status_code == 200 else []

        # Delete matching secrets (flexible URL matching)
        for s in secrets:
            url = s.get("url", "")
            secret_id = s.get("secret_id", "")
            if _norm(url) in delete_normalized and secret_id:
                del_resp = await client.delete(
                    f"https://api.gumloop.com//secrets/mcp_servers/{secret_id}",
                    headers=headers,
                )
                if del_resp.status_code in (200, 204):
                    deleted_secrets += 1
                else:
                    errors.append(f"{url}: HTTP {del_resp.status_code}")

        # Also remove from gummie tools if gummie exists
        if gummie_id:
            resp2 = await client.get(f"https://api.gumloop.com/gummies/{gummie_id}", headers=headers)
            if resp2.status_code == 200:
                current_tools = resp2.json().get("gummie", {}).get("tools", [])
                new_tools = [t for t in current_tools
                             if not (t.get("type") == "mcp_server" and _norm(t.get("mcp_server_url", "")) in delete_normalized)]
                if len(new_tools) != len(current_tools):
                    await client.patch(
                        f"https://api.gumloop.com/gummies/{gummie_id}",
                        json={"tools": new_tools}, headers=headers,
                    )

    # Persist refreshed tokens
    updated = auth.get_updated_tokens()
    if updated.get("gl_id_token"):
        try:
            await db.update_account(account_id, **updated)
        except Exception:
            pass

    return {"ok": True, "deleted": deleted_secrets, "errors": errors}


@router.post("/accounts/mcp-delete-bulk")
async def mcp_delete_bulk_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Delete MCP server(s) from ALL Gumloop accounts by URL.

    Body: {"urls": ["url1", "url2"]}
    """
    body = await request.json()
    delete_urls = body.get("urls", [])
    if not delete_urls:
        return JSONResponse({"error": "urls list is required"}, status_code=400)

    def _norm(u):
        u = u.rstrip("/")
        if u.endswith("/mcp"):
            u = u[:-4]
        return u.rstrip("/")

    delete_normalized = {_norm(u) for u in delete_urls}

    all_accts = await db.get_accounts()
    gl_accounts = [a for a in all_accts
                   if a.get("gl_status") == "ok" and a.get("gl_refresh_token")]

    if not gl_accounts:
        return {"ok": True, "total": 0, "message": "No GL accounts found"}

    import httpx as _httpx
    results = []
    total_deleted = 0
    for acct in gl_accounts:
        refresh_tok = acct.get("gl_refresh_token", "")
        gummie_id = acct.get("gl_gummie_id", "")
        if not refresh_tok:
            continue

        from .gumloop.auth import GumloopAuth
        proxy_info = await db.get_proxy_for_batch()
        proxy_url = proxy_info["url"] if proxy_info else None

        auth = GumloopAuth(
            refresh_token=refresh_tok,
            user_id=acct.get("gl_user_id", ""),
            id_token=acct.get("gl_id_token", ""),
            proxy_url=proxy_url,
        )
        try:
            id_token = await auth.get_token()
        except Exception:
            results.append({"email": acct.get("email", "?"), "ok": False, "error": "Token refresh failed"})
            continue

        user_id = auth.user_id
        hdrs = {"Authorization": f"Bearer {id_token}", "x-auth-key": user_id, "Content-Type": "application/json"}
        ckw = {"timeout": 30}
        if proxy_url:
            ckw["proxy"] = proxy_url

        acct_deleted = 0
        async with _httpx.AsyncClient(**ckw) as client:
            resp = await client.get("https://api.gumloop.com//secrets/mcp_servers", headers=hdrs)
            secrets = resp.json() if resp.status_code == 200 else []

            for s in secrets:
                url = s.get("url", "")
                secret_id = s.get("secret_id", "")
                if _norm(url) in delete_normalized and secret_id:
                    dr = await client.delete(f"https://api.gumloop.com//secrets/mcp_servers/{secret_id}", headers=hdrs)
                    if dr.status_code in (200, 204):
                        acct_deleted += 1

            # Remove from gummie tools
            if gummie_id:
                resp2 = await client.get(f"https://api.gumloop.com/gummies/{gummie_id}", headers=hdrs)
                if resp2.status_code == 200:
                    tools = resp2.json().get("gummie", {}).get("tools", [])
                    new_tools = [t for t in tools if not (t.get("type") == "mcp_server" and _norm(t.get("mcp_server_url", "")) in delete_normalized)]
                    if len(new_tools) != len(tools):
                        await client.patch(f"https://api.gumloop.com/gummies/{gummie_id}", json={"tools": new_tools}, headers=hdrs)

        # Persist tokens
        updated = auth.get_updated_tokens()
        if updated.get("gl_id_token"):
            try:
                await db.update_account(acct["id"], **updated)
            except Exception:
                pass

        total_deleted += acct_deleted
        results.append({"email": acct.get("email", "?"), "ok": True, "deleted": acct_deleted})

        import asyncio as _aio
        await _aio.sleep(0.3)

    return {"ok": True, "total_accounts": len(gl_accounts), "total_deleted": total_deleted, "results": results}


@router.post("/accounts/{account_id}/bind-mcp")
async def bind_mcp_endpoint(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Bind MCP server(s) to a single Gumloop account's gummie."""
    body = await request.json()
    mcp_urls = body.get("mcp_urls", [])
    if not mcp_urls or not isinstance(mcp_urls, list):
        return JSONResponse({"error": "mcp_urls (list) is required"}, status_code=400)

    mcp_urls = [u.strip() for u in mcp_urls if isinstance(u, str) and u.strip()]
    if not mcp_urls:
        return JSONResponse({"error": "No valid MCP URLs provided"}, status_code=400)

    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)

    if not account.get("gl_gummie_id") or not account.get("gl_refresh_token"):
        return JSONResponse({"error": "Account has no Gumloop gummie or refresh token"}, status_code=400)

    proxy_info = await db.get_proxy_for_batch()
    proxy_url = proxy_info["url"] if proxy_info else None
    proxy_display = proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else (proxy_url or "direct")

    from .batch_runner import attach_mcp_to_account
    result = await attach_mcp_to_account(account, mcp_urls, proxy_url=proxy_url)
    result["proxy"] = proxy_display

    if result.get("error"):
        return JSONResponse({"error": result["error"], "proxy": proxy_display}, status_code=500)
    return result


@router.post("/accounts/bind-mcp-bulk")
async def bind_mcp_bulk_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Bind MCP server(s) to multiple Gumloop accounts at once.

    Uses batch proxy pool with smart rotate support.
    Returns results array with per-account status.
    """
    import asyncio as _asyncio

    body = await request.json()
    mcp_urls = body.get("mcp_urls", [])
    account_ids = body.get("account_ids", [])

    if not mcp_urls or not isinstance(mcp_urls, list):
        return JSONResponse({"error": "mcp_urls (list) is required"}, status_code=400)
    mcp_urls = [u.strip() for u in mcp_urls if isinstance(u, str) and u.strip()]
    if not mcp_urls:
        return JSONResponse({"error": "No valid MCP URLs provided"}, status_code=400)

    # Get accounts — either specified IDs or all active GL accounts
    if account_ids:
        accounts = []
        for aid in account_ids:
            acct = await db.get_account(int(aid))
            if acct and acct.get("gl_gummie_id") and acct.get("gl_refresh_token"):
                accounts.append(acct)
    else:
        all_accts = await db.get_accounts()
        accounts = [a for a in all_accts
                    if a.get("gl_status") == "ok" and a.get("gl_gummie_id") and a.get("gl_refresh_token")]

    if not accounts:
        return JSONResponse({"error": "No eligible GL accounts found"}, status_code=400)

    from .batch_runner import attach_mcp_to_account

    results = []
    for acct in accounts:
        # Get proxy per-account (smart rotate gives different proxy each time)
        proxy_info = await db.get_proxy_for_batch()
        proxy_url = proxy_info["url"] if proxy_info else None
        proxy_display = proxy_url.split("@")[-1] if proxy_url and "@" in proxy_url else (proxy_url or "direct")

        try:
            result = await attach_mcp_to_account(acct, mcp_urls, proxy_url=proxy_url)
            result["email"] = acct.get("email", "?")
            result["proxy"] = proxy_display
            results.append(result)
        except Exception as e:
            results.append({
                "email": acct.get("email", "?"),
                "ok": False,
                "error": str(e),
                "proxy": proxy_display,
            })

        # Small delay to avoid rate limiting
        await _asyncio.sleep(0.3)

    ok_count = sum(1 for r in results if r.get("ok"))
    return {"ok": True, "total": len(accounts), "success": ok_count, "results": results}


@router.delete("/accounts/delete-fix")
async def delete_fix_accounts(request: Request, _: bool = Depends(verify_admin)):
    """Per-provider fix cleanup: clear dead provider credentials.

    If `provider` is specified in body, only clear that provider's credentials.
    Otherwise clear all providers.

    For each account, check each provider:
    - If provider is verified-fix (exhausted/banned/failed + verified=1), clear its credentials.
    - If ALL providers end up dead after cleanup, delete the whole account.

    This allows keeping accounts that still have working providers.
    """
    # Parse optional provider filter from request body
    target_provider = None
    try:
        body = await request.json()
        target_provider = body.get("provider")
    except Exception:
        pass

    accounts = await db.get_accounts()
    cleared = 0
    deleted = 0

    for acc in accounts:
        acct_id = acc["id"]
        any_cleared = False

        # Kiro: fix = verified + dead status
        if (not target_provider or target_provider == "kiro") and \
           acc.get("kiro_verified", 0) == 1 and acc.get("kiro_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, kiro_status="none", kiro_access_token="",
                                    kiro_refresh_token="", kiro_error="", kiro_error_count=0,
                                    kiro_credits=0, kiro_credits_total=0, kiro_credits_used=0,
                                    kiro_verified=0)
            any_cleared = True
            cleared += 1

        # CodeBuddy: fix = verified + dead status (including rate_limited)
        if (not target_provider or target_provider == "codebuddy") and \
           acc.get("cb_verified", 0) == 1 and acc.get("cb_status") in ("failed", "exhausted", "banned", "rate_limited"):
            await db.update_account(acct_id, cb_status="none", cb_api_key="",
                                    cb_error="", cb_error_count=0, cb_credits=0, cb_verified=0)
            any_cleared = True
            cleared += 1

        # WaveSpeed: fix = verified + dead status
        if (not target_provider or target_provider == "wavespeed") and \
           acc.get("ws_verified", 0) == 1 and acc.get("ws_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, ws_status="none", ws_api_key="",
                                    ws_error="", ws_error_count=0, ws_credits=0, ws_verified=0)
            any_cleared = True
            cleared += 1

        # Gumloop: fix = verified + dead status
        if (not target_provider or target_provider == "gumloop") and \
           acc.get("gl_verified", 0) == 1 and acc.get("gl_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, gl_status="none", gl_refresh_token="", gl_id_token="",
                                    gl_user_id="", gl_gummie_id="", gl_error="", gl_error_count=0,
                                    gl_verified=0)
            any_cleared = True
            cleared += 1

        # ChatBAI: fix = verified + dead status
        if (not target_provider or target_provider == "cbai") and \
           acc.get("cbai_verified", 0) == 1 and acc.get("cbai_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, cbai_status="none", cbai_api_key="", cbai_session_token="",
                                    cbai_error="", cbai_error_count=0, cbai_credits=0, cbai_verified=0)
            any_cleared = True
            cleared += 1

        # SkillBoss: fix = verified + dead status
        if (not target_provider or target_provider == "skillboss") and \
           acc.get("skboss_verified", 0) == 1 and acc.get("skboss_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, skboss_status="none", skboss_api_key="",
                                    skboss_error="", skboss_error_count=0, skboss_credits=0, skboss_verified=0)
            any_cleared = True
            cleared += 1

        # Windsurf: fix = verified + dead status
        if (not target_provider or target_provider == "windsurf") and \
           acc.get("windsurf_verified", 0) == 1 and acc.get("windsurf_status") in ("failed", "exhausted", "banned"):
            await db.update_account(acct_id, windsurf_status="none", windsurf_api_key="",
                                    windsurf_error="", windsurf_error_count=0, windsurf_credits=0,
                                    windsurf_verified=0)
            any_cleared = True
            cleared += 1

        # After cleanup, check if account has ANY alive provider left
        if any_cleared:
            refreshed = await db.get_account(acct_id)
            if refreshed:
                has_any = (
                    (refreshed.get("kiro_status") == "ok" and refreshed.get("kiro_access_token"))
                    or (refreshed.get("cb_status") == "ok" and refreshed.get("cb_api_key"))
                    or (refreshed.get("ws_status") == "ok" and refreshed.get("ws_api_key"))
                    or (refreshed.get("gl_status") == "ok" and refreshed.get("gl_refresh_token"))
                    or (refreshed.get("cbai_status") == "ok" and refreshed.get("cbai_api_key"))
                    or (refreshed.get("skboss_status") == "ok" and refreshed.get("skboss_api_key"))
                    or (refreshed.get("windsurf_status") == "ok" and refreshed.get("windsurf_api_key"))
                )
                # Also check if any provider is still pending/none (could be re-logged)
                has_pending = (
                    refreshed.get("kiro_status") in ("none", "pending")
                    or refreshed.get("cb_status") in ("none", "pending")
                    or refreshed.get("ws_status") in ("none", "pending")
                    or refreshed.get("gl_status") in ("none", "pending")
                    or refreshed.get("cbai_status") in ("none", "pending")
                    or refreshed.get("skboss_status") in ("none", "pending")
                    or refreshed.get("windsurf_status") in ("none", "pending")
                )
                if not has_any and not has_pending:
                    try:
                        from . import license_client
                        await license_client.d1_delete_account(acc["email"])
                    except Exception:
                        pass
                    await db.delete_account(acct_id)
                    deleted += 1

    return {"ok": True, "cleared": cleared, "deleted": deleted}


@router.post("/accounts/{account_id}/retry")
async def retry_login(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Retry failed login for an account."""
    result = await retry_account(account_id)
    if "error" in result:
        return JSONResponse({"error": result["error"]}, status_code=404)
    return result


@router.put("/accounts/{account_id}")
async def update_account_fields(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Update account fields directly (for manual status changes)."""
    body = await request.json()
    allowed = {
        "status", "kiro_status", "kiro_error", "kiro_error_count",
        "cb_status", "cb_error", "cb_error_count",
        "ws_status", "ws_error", "ws_error_count",
        "gl_status", "gl_error", "gl_error_count",
        "skboss_status", "skboss_error", "skboss_error_count",
        "windsurf_status", "windsurf_error", "windsurf_error_count",
    }
    fields = {k: v for k, v in body.items() if k in allowed}
    if not fields:
        return JSONResponse({"error": "No valid fields"}, status_code=400)
    ok = await db.update_account(account_id, **fields)
    if not ok:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    try:
        from . import license_client
        await license_client.d1_sync_account(account_id)
    except Exception:
        pass
    return {"ok": True}


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Permanently delete an account. Pushes deletion to D1 first."""
    account = await db.get_account(account_id)
    if not account:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    # Delete from D1 first (D1 = source of truth)
    try:
        from . import license_client
        await license_client.d1_delete_account(account["email"])
    except Exception:
        pass
    # Then delete locally
    await db.delete_account(account_id)
    return {"ok": True}


@router.post("/accounts/{account_id}/trash")
async def trash_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Move account to trash."""
    ok = await db.move_to_trash(account_id)
    if not ok:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    try:
        from . import license_client
        await license_client.d1_sync_account(account_id)
    except Exception:
        pass
    return {"ok": True}


@router.post("/accounts/{account_id}/restore")
async def restore_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Restore account from trash."""
    ok = await db.restore_account(account_id)
    if not ok:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    try:
        from . import license_client
        await license_client.d1_sync_account(account_id)
    except Exception:
        pass
    return {"ok": True}


@router.post("/accounts/sync-d1/push")
async def sync_d1_push(request: Request, _: bool = Depends(verify_admin)):
    """Step 1: Push all local accounts to D1 (chunked, with progress)."""
    from . import license_client
    if not license_client.is_licensed():
        return JSONResponse({"error": "Not licensed"}, status_code=400)

    local_accounts = await db.get_accounts()

    # Push in smaller chunks to avoid D1 timeout
    CHUNK = 30
    total_pushed = 0
    errors = []
    for i in range(0, len(local_accounts), CHUNK):
        chunk = local_accounts[i:i + CHUNK]
        try:
            result = await license_client.push_sync(accounts=chunk)
            if result.get("error"):
                errors.append(f"Chunk {i}-{i+len(chunk)}: {result['error']}")
            else:
                total_pushed += result.get("accounts_upserted", 0)
        except Exception as e:
            errors.append(f"Chunk {i}-{i+len(chunk)}: {e}")
        # Small delay between chunks
        import asyncio
        await asyncio.sleep(0.3)

    if errors and total_pushed == 0:
        return JSONResponse({"error": f"Push failed: {'; '.join(errors[:3])}"}, status_code=502)

    return {"ok": True, "pushed": total_pushed, "total_local": len(local_accounts), "errors": errors[:5] if errors else []}


@router.get("/accounts/sync-d1/preview")
async def sync_d1_preview(request: Request, _: bool = Depends(verify_admin)):
    """Step 2: Preview — compare local vs D1, show what would change."""
    from . import license_client
    if not license_client.is_licensed():
        return JSONResponse({"error": "Not licensed"}, status_code=400)

    # Get local counts
    local_accounts = await db.get_accounts()
    local_emails = {a["email"] for a in local_accounts}
    l_kr = sum(1 for a in local_accounts if a.get("kiro_status") == "ok")
    l_cb = sum(1 for a in local_accounts if a.get("cb_status") == "ok")
    l_ws = sum(1 for a in local_accounts if a.get("ws_status") == "ok")
    l_gl = sum(1 for a in local_accounts if a.get("gl_status") == "ok")
    l_cbai = sum(1 for a in local_accounts if a.get("cbai_status") == "ok")
    l_skb = sum(1 for a in local_accounts if a.get("skboss_status") == "ok")
    l_wf = sum(1 for a in local_accounts if a.get("windsurf_status") == "ok")

    # Get D1 counts
    d1_result = await license_client._api_get("/api/sync/pull", {
        "license_key": license_client.LICENSE_KEY,
        "device_fingerprint": license_client._device_fingerprint,
    })
    if d1_result.get("error"):
        return JSONResponse({"error": f"D1 pull failed: {d1_result['error']}"}, status_code=502)

    d1_accounts = d1_result.get("accounts", [])
    d_kr = sum(1 for a in d1_accounts if a.get("kiro_status") == "ok")
    d_cb = sum(1 for a in d1_accounts if a.get("cb_status") == "ok")
    d_ws = sum(1 for a in d1_accounts if a.get("ws_status") == "ok")
    d_gl = sum(1 for a in d1_accounts if a.get("gl_status") == "ok")
    d_cbai = sum(1 for a in d1_accounts if a.get("cbai_status") == "ok")
    d_skb = sum(1 for a in d1_accounts if a.get("skboss_status") == "ok")
    d_wf = sum(1 for a in d1_accounts if a.get("windsurf_status") == "ok")

    # Count new accounts (in D1 but not local)
    new_from_d1 = sum(1 for a in d1_accounts
                      if a.get("email") and a["email"] not in local_emails
                      and a.get("status") != "deleted")

    return {
        "ok": True,
        "local": {"kr": l_kr, "cb": l_cb, "ws": l_ws, "gl": l_gl, "cbai": l_cbai, "skb": l_skb, "total": len(local_accounts)},
        "d1": {"kr": d_kr, "cb": d_cb, "ws": d_ws, "gl": d_gl, "cbai": d_cbai, "skb": d_skb, "total": len(d1_accounts)},
        "new_from_d1": new_from_d1,
    }


@router.post("/accounts/sync-d1/pull")
async def sync_d1_pull(request: Request, _: bool = Depends(verify_admin)):
    """Step 3: Pull new accounts from D1 (user confirmed)."""
    from . import license_client
    if not license_client.is_licensed():
        return JSONResponse({"error": "Not licensed"}, status_code=400)

    pull_result = await license_client.pull_new_accounts_only()
    new_accounts = pull_result.get("new_accounts", 0)

    all_accounts = await db.get_accounts()
    kr = sum(1 for a in all_accounts if a.get("kiro_status") == "ok")
    cb = sum(1 for a in all_accounts if a.get("cb_status") == "ok")
    ws = sum(1 for a in all_accounts if a.get("ws_status") == "ok")
    gl = sum(1 for a in all_accounts if a.get("gl_status") == "ok")

    return {
        "ok": True,
        "new_accounts": new_accounts,
        "total": len(all_accounts),
        "kr": kr, "cb": cb, "ws": ws, "gl": gl,
    }


@router.get("/accounts/gl/check-disabled")
async def check_gl_disabled(request: Request, _: bool = Depends(verify_admin)):
    """Check all GL accounts with gl_status=ok for USER_DISABLED. Marks + returns results."""
    all_accts = await db.get_accounts()
    gl_accts = [a for a in all_accts
                if a.get("gl_status") == "ok" and a.get("gl_refresh_token")]

    from .gumloop.auth import GumloopAuth, UserDisabledError as _UserDisabledError
    import httpx as _httpx

    results: list[dict] = []
    for acct in gl_accts:
        acct_id = acct["id"]
        refresh_tok = acct.get("gl_refresh_token", "")
        auth = GumloopAuth(
            refresh_token=refresh_tok,
            user_id=acct.get("gl_user_id", ""),
            id_token=acct.get("gl_id_token", ""),
        )
        try:
            await auth.get_token()
            results.append({"id": acct_id, "email": acct.get("email", "?"),
                            "status": "ok"})
        except _UserDisabledError:
            # Mark account in DB
            await db.update_account(acct_id, gl_status="user_disabled",
                                    gl_error="USER_DISABLED")
            results.append({"id": acct_id, "email": acct.get("email", "?"),
                            "status": "user_disabled", "error": "USER_DISABLED"})
            log.warning("GL account %s (%s) marked as user_disabled",
                        acct_id, acct.get("email", "?"))
        except Exception as exc:
            results.append({"id": acct_id, "email": acct.get("email", "?"),
                            "status": "error", "error": str(exc)[:200]})

    disabled_count = sum(1 for r in results if r["status"] == "user_disabled")
    ok_count = sum(1 for r in results if r["status"] == "ok")
    return {
        "ok": True,
        "total_checked": len(results),
        "working": ok_count,
        "user_disabled": disabled_count,
        "other_errors": len(results) - ok_count - disabled_count,
        "results": results,
    }


@router.post("/accounts/gl/check-disabled/relogin")
async def relogin_gl_disabled(request: Request, _: bool = Depends(verify_admin)):
    """Trigger batch re-login for all GL accounts marked as user_disabled."""
    all_accts = await db.get_accounts()
    disabled = [a for a in all_accts if a.get("gl_status") == "user_disabled"]

    if not disabled:
        return {"ok": True, "message": "No user_disabled accounts found", "queued": 0}

    # Build (email, password) tuples — password may be empty, batch runner will use stored tokens
    accounts: list[tuple[str, str]] = []
    for acct in disabled:
        email = acct.get("email", "")
        password = acct.get("password", "")
        if email:
            accounts.append((email, password))

    if not accounts:
        return {"ok": False, "message": "No accounts with email found", "queued": 0}

    # Mark them back to "none" so batch runner does full signup flow
    for acct in disabled:
        await db.update_account(acct["id"], gl_status="none", gl_error="",
                                gl_refresh_token="", gl_id_token="", gl_user_id="")

    from .batch_runner import start_batch as _start_batch
    queued = await _start_batch(accounts, providers=["gumloop"],
                                 headless=True, concurrency=2)

    return {
        "ok": True,
        "message": f"Queued {queued} accounts for re-login",
        "queued": queued,
        "accounts": [a[0] for a in accounts],
    }


@router.get("/accounts/refresh-credits")
async def refresh_all_credits_get(request: Request, _: bool = Depends(verify_admin)):
    """Refresh credits for all active accounts (GET)."""
    results = await _refresh_all_credits()
    trashed = await auto_trash()
    return {"results": results, "auto_trashed": trashed}


@router.post("/accounts/refresh-credits")
async def refresh_all_credits_post(request: Request, _: bool = Depends(verify_admin)):
    """Refresh credits for all active accounts (POST)."""
    results = await _refresh_all_credits()
    trashed = await auto_trash()
    return {"results": results, "auto_trashed": trashed}


@router.post("/accounts/{account_id}/refresh")
async def refresh_single_account(account_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Refresh credits for a single account."""
    account = await db.get_account(account_id)
    if account is None:
        return JSONResponse({"error": "Account not found"}, status_code=404)
    result = await refresh_credits(account_id)
    return {"id": account_id, "email": account["email"], **result}


# ---------------------------------------------------------------------------
# Usage logs endpoints
# ---------------------------------------------------------------------------

@router.get("/logs")
async def get_logs(request: Request, _: bool = Depends(verify_admin), limit: int = 50):
    """Return recent usage logs with headers+body."""
    logs = await db.get_usage_logs(limit=min(limit, 500))
    return {"logs": logs, "count": len(logs)}


@router.get("/logs/{log_id}")
async def get_log_detail(log_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Return a single usage log with full detail."""
    entry = await db.get_usage_log(log_id)
    if entry is None:
        return JSONResponse({"error": "Log not found"}, status_code=404)
    return entry


# ---------------------------------------------------------------------------
# API key endpoints
# ---------------------------------------------------------------------------

@router.get("/keys")
async def list_keys(request: Request, _: bool = Depends(verify_admin)):
    """List all API keys (without full key values)."""
    keys = await db.get_api_keys()
    return {
        "keys": [
            {
                "id": k["id"],
                "key_prefix": k["key_prefix"],
                "name": k["name"],
                "active": bool(k["active"]),
                "created_at": k["created_at"],
                "last_used": k["last_used"],
                "usage_count": k["usage_count"],
            }
            for k in keys
        ]
    }


@router.post("/keys/generate")
async def generate_key(req: ApiKeyCreate, request: Request, _: bool = Depends(verify_admin)):
    """Generate a new API key. Returns the full key only once."""
    key_id, full_key = await db.create_api_key(req.name)
    return {
        "id": key_id,
        "key": full_key,
        "name": req.name,
        "message": "Save this key — it will not be shown again.",
    }


@router.delete("/keys/{key_id}")
async def revoke_key(key_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Revoke (delete) an API key."""
    ok = await db.revoke_api_key(key_id)
    if not ok:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return {"ok": True}


@router.post("/keys/{key_id}/regenerate")
async def regenerate_key(key_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Regenerate an API key. Returns the new full key only once."""
    new_key = await db.regenerate_api_key(key_id)
    if new_key is None:
        return JSONResponse({"error": "Key not found"}, status_code=404)
    return {
        "id": key_id,
        "key": new_key,
        "message": "Save this key — it will not be shown again.",
    }


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

@router.get("/settings/captcha")
async def get_captcha_settings(_: bool = Depends(verify_admin)):
    """Get captcha API key (masked) and stats."""
    key = await db.get_setting("captcha_api_key", "")
    from .proxy_gumloop import get_captcha_stats
    stats = get_captcha_stats()
    return {
        "has_key": bool(key),
        "key_preview": key[:6] + "***" + key[-4:] if len(key) > 10 else ("***" if key else ""),
        **stats,
    }


@router.post("/settings/captcha")
async def set_captcha_settings(request: Request, _: bool = Depends(verify_admin)):
    """Set captcha API key. Body: {api_key: str}."""
    body = await request.json()
    api_key = str(body.get("api_key", "")).strip()
    if not api_key:
        return JSONResponse({"error": "api_key required"}, status_code=400)
    await db.set_setting("captcha_api_key", api_key)
    # Update the live turnstile solver
    from .proxy_gumloop import _get_turnstile
    ts = _get_turnstile()
    ts.update_api_key(api_key)
    return {"ok": True, "has_key": True, "key_preview": api_key[:6] + "***" + api_key[-4:]}


@router.delete("/settings/captcha")
async def clear_captcha_settings(_: bool = Depends(verify_admin)):
    """Clear captcha API key."""
    await db.set_setting("captcha_api_key", "")
    from .proxy_gumloop import _get_turnstile
    ts = _get_turnstile()
    ts.update_api_key("")
    return {"ok": True}


@router.get("/stats")
async def get_stats(request: Request, _: bool = Depends(verify_admin)):
    """Usage statistics."""
    stats = await db.get_usage_stats()
    # Add captcha stats
    from .proxy_gumloop import get_captcha_stats
    stats["captcha"] = get_captcha_stats()
    return stats


# ---------------------------------------------------------------------------
# SSE events (batch progress)
# ---------------------------------------------------------------------------

@router.get("/events")
async def sse_events(request: Request, _: bool = Depends(verify_admin)):
    """SSE stream for batch login progress.

    Includes padding to prevent Cloudflare/proxy buffering of small SSE chunks.
    """
    queue = batch_state.subscribe()

    # Padding comment to force Cloudflare to flush (>1KB initial payload)
    _FLUSH_PADDING = ": " + "x" * 2048 + "\n\n"

    async def event_stream():
        try:
            # Send initial padding to bust proxy buffers
            yield _FLUSH_PADDING
            yield ": connected\n\n"

            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                    data = json.dumps(event, ensure_ascii=False)
                    # Pad small events to >256 bytes to prevent buffering
                    padding = ""
                    if len(data) < 256:
                        padding = " " * (256 - len(data))
                    yield f"data: {data}{padding}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            batch_state.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Content-Type-Options": "nosniff",
            "Transfer-Encoding": "chunked",
        },
    )


# ---------------------------------------------------------------------------
# Batch control
# ---------------------------------------------------------------------------

@router.post("/batch/start")
async def start_batch_endpoint(req: BatchLoginRequest, request: Request, _: bool = Depends(verify_admin)):
    """Start batch login."""
    # Validate providers FIRST (needed before auto-gen check)
    providers = [p for p in req.providers if p in ("kiro", "codebuddy", "wavespeed", "gumloop", "chatbai", "skillboss", "therouter", "windsurf", "windsurf-emailpass", "windsurf-google")]
    if not providers:
        return JSONResponse({"error": "No valid providers"}, status_code=400)

    # Parse accounts from textarea
    accounts: list[tuple[str, str]] = []
    for line in req.accounts:
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        accounts.append((parts[0].strip(), parts[1].strip()))

    # TheRouter auto-gen: generate placeholder accounts if textarea is empty
    if not accounts and "therouter" in providers:
        import random, string
        tr_count = max(1, min(100, req.tr_count))
        for _ in range(tr_count):
            placeholder_email = f"tr-auto-{random.randint(10000,99999)}@placeholder"
            placeholder_pass = "".join(random.choices(string.ascii_letters + string.digits, k=14))
            accounts.append((placeholder_email, placeholder_pass))

    if not accounts:
        return JSONResponse({"error": "No valid accounts"}, status_code=400)

    mcp_urls = [u.strip() for u in (req.mcp_urls or []) if u.strip()]

    try:
        count = await start_batch(accounts, providers, headless=req.headless,
                                   concurrency=max(1, req.concurrency),
                                   mcp_urls=mcp_urls,
                                   gumloop_mode=getattr(req, 'gumloop_mode', 'full'))
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)

    return {"ok": True, "count": count}


@router.get("/batch/status")
async def batch_status_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Get batch status including failed jobs (not saved to DB)."""
    jobs = []
    failed_jobs = []
    for j in batch_state.jobs:
        job_info = {
            "id": j.id,
            "email": j.email,
            "providers": j.providers,
            "status": j.status,
            "logs_count": len(j.logs),
            "in_db": j.account_id is not None,
            "proxy_used": getattr(j, '_proxy_used', ''),
        }
        jobs.append(job_info)
        if j.status == "failed" and j.account_id is None:
            errors = {}
            if j.result:
                for prov in j.providers:
                    prov_result = j.result.get(prov, {})
                    if not prov_result.get("success"):
                        errors[prov] = prov_result.get("error", "Unknown error")
            failed_jobs.append({
                "email": j.email,
                "errors": errors,
                "providers": j.providers,
            })
    # Timing info
    started_times = [j.started_at for j in batch_state.jobs if j.started_at > 0]
    finished_times = [j.finished_at for j in batch_state.jobs if j.finished_at > 0]

    return {
        "running": batch_state.running,
        "jobs": jobs,
        "failed_jobs": failed_jobs,
        "started_at": min(started_times) if started_times else 0,
        "finished_at": max(finished_times) if finished_times else 0,
    }


@router.post("/batch/cancel")
async def cancel_batch_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Cancel running batch."""
    cancel_batch()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Test model endpoints
# ---------------------------------------------------------------------------

async def _test_single_model(model: str, admin_password: str) -> dict:
    """Send a minimal test request to the proxy for a given model."""
    tier_enum = MODEL_TIER.get(model)
    tier_str = tier_enum.value if tier_enum else "unknown"

    # We need an API key to call the proxy. Get the first active one.
    keys = await db.get_api_keys()
    if not keys:
        return {
            "success": False, "model": model, "tier": tier_str,
            "latency_ms": 0, "response_headers": {}, "response_body": "",
            "error": "No API keys available for testing",
        }

    # Use the first active key's hash to find the actual key — we can't recover it.
    # Instead, generate a temporary key for testing.
    test_key_id, test_key = await db.create_api_key("_test_temp")

    start = time.monotonic()
    result: dict = {
        "success": False, "model": model, "tier": tier_str,
        "latency_ms": 0, "response_headers": {}, "response_body": "", "error": "",
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"http://127.0.0.1:{LISTEN_PORT}/v1/chat/completions",
                json={
                    "model": model,
                    "messages": [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say OK"}],
                    "stream": False,
                    "max_tokens": 100,
                },
                headers={
                    "Authorization": f"Bearer {test_key}",
                    "Content-Type": "application/json",
                },
            )
            latency = int((time.monotonic() - start) * 1000)
            result["latency_ms"] = latency
            result["response_headers"] = dict(resp.headers)
            body_text = resp.text[:2000]
            result["response_body"] = body_text

            if resp.status_code == 200:
                result["success"] = True
            else:
                result["error"] = f"HTTP {resp.status_code}: {body_text[:500]}"

    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        result["latency_ms"] = latency
        result["error"] = str(e)
    finally:
        # Clean up temp key
        await db.revoke_api_key(test_key_id)

    return result


@router.post("/test-model")
async def test_model(req: TestModelRequest, request: Request, _: bool = Depends(verify_admin)):
    """Test a single model by sending a minimal request through the proxy."""
    admin_pw = (
        request.headers.get("X-Admin-Password", "")
        or request.cookies.get("admin_password", "")
        or request.query_params.get("password", "")
    )
    result = await _test_single_model(req.model, admin_pw)
    return result


@router.post("/test-all-models")
async def test_all_models(request: Request, _: bool = Depends(verify_admin)):
    """Test all models sequentially."""
    admin_pw = (
        request.headers.get("X-Admin-Password", "")
        or request.cookies.get("admin_password", "")
        or request.query_params.get("password", "")
    )
    results: list[dict] = []
    for model_name in MODEL_TIER:
        if model_name in _HIDDEN_ALIASES:
            continue
        result = await _test_single_model(model_name, admin_pw)
        results.append(result)
    return {"results": results, "total": len(results), "success": sum(1 for r in results if r["success"])}


# ---------------------------------------------------------------------------
# Proxy config
# ---------------------------------------------------------------------------

@router.get("/proxy")
async def get_proxy(purpose: str = "api", _: bool = Depends(verify_admin)):
    """Get proxy config + all proxies, optionally filtered by purpose."""
    proxies = await db.get_proxies(purpose=purpose)
    if purpose == "batch":
        enabled = (await db.get_setting("batch_proxy_enabled", "false")).lower() == "true"
        smart_rotate = (await db.get_setting("batch_smart_rotate", "true")).lower() == "true"
    else:
        enabled = (await db.get_setting("proxy_enabled", "false")).lower() == "true"
        smart_rotate = (await db.get_setting("api_proxy_smart_rotate", "true")).lower() == "true"
    return {"proxies": proxies, "enabled": enabled, "smart_rotate": smart_rotate, "purpose": purpose}


@router.post("/proxy/toggle")
async def toggle_proxy_mode(request: Request, _: bool = Depends(verify_admin)):
    """Enable/disable proxy mode. Body: {enabled: bool}."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    await db.set_proxy_config(enabled)

    from .proxy_kiro import close_all_clients as close_kiro
    from .proxy_codebuddy import close_all_clients as close_cb
    from .proxy_wavespeed import close_all_clients as close_ws
    await close_kiro()
    await close_cb()
    await close_ws()

    return {"ok": True, "enabled": enabled}


def _detect_proxy_type(url: str) -> str:
    """Auto-detect proxy type from URL scheme."""
    lower = url.lower()
    if lower.startswith("socks5://") or lower.startswith("socks5h://"):
        return "socks5"
    if lower.startswith("socks4://"):
        return "socks4"
    return "http"


def _normalize_proxy_url(raw: str) -> str:
    """Normalize proxy URL format.

    Accepts:  scheme://host:port:user:pass
    Converts: scheme://user:pass@host:port

    Also accepts standard format (scheme://user:pass@host:port) as-is.
    """
    raw = raw.strip()
    if not raw:
        return raw
    # Already has @ → standard format, leave as-is
    if "@" in raw:
        return raw
    # Split scheme from rest
    if "://" in raw:
        scheme, rest = raw.split("://", 1)
    else:
        scheme, rest = "http", raw
    # Split by colon: host:port or host:port:user:pass
    parts = rest.split(":")
    if len(parts) == 4:
        # host:port:user:pass → user:pass@host:port
        host, port, user, passwd = parts
        return f"{scheme}://{user}:{passwd}@{host}:{port}"
    elif len(parts) == 2:
        # host:port (no auth)
        return f"{scheme}://{rest}"
    else:
        # Unknown format, return as-is
        return raw


async def _test_proxy_url(url: str, timeout: int = 10) -> tuple[bool, int, str]:
    """Test a proxy URL. Returns (ok, latency_ms, error)."""
    import time as _time
    try:
        async with httpx.AsyncClient(proxy=url, timeout=timeout) as client:
            t0 = _time.monotonic()
            resp = await client.get("https://httpbin.org/ip")
            latency = int((_time.monotonic() - t0) * 1000)
        return True, latency, ""
    except Exception as e:
        return False, -1, str(e)[:200]


@router.post("/proxy/add")
async def add_proxy(request: Request, _: bool = Depends(verify_admin)):
    """Add proxy(s) with auto-test. Failed proxies are skipped.

    Body: {url, label?, type?, purpose?} or {urls: 'line\\nline', purpose?} for bulk.
    """
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    skip_test = bool(body.get("skip_test", False))

    # Bulk mode: urls field with newline-separated proxies
    urls_raw = str(body.get("urls", "")).strip()
    if urls_raw:
        lines = [l.strip() for l in urls_raw.splitlines() if l.strip()]
        if not lines:
            return JSONResponse({"error": "No valid proxy URLs"}, status_code=400)
        added = []
        failed = []
        for line in lines:
            line = _normalize_proxy_url(line)
            ptype = _detect_proxy_type(line)
            if not skip_test:
                ok, latency, err = await _test_proxy_url(line)
                if not ok:
                    failed.append({"url": line, "type": ptype, "error": err})
                    continue
                pid = await db.add_proxy(line, "", ptype, purpose=purpose)
                await db.update_proxy_test(pid, latency)
                added.append({"id": pid, "url": line, "type": ptype, "latency_ms": latency})
            else:
                pid = await db.add_proxy(line, "", ptype, purpose=purpose)
                added.append({"id": pid, "url": line, "type": ptype})
        return {"ok": True, "added": added, "failed": failed,
                "count": len(added), "failed_count": len(failed)}

    # Single mode (backward compat)
    url = _normalize_proxy_url(str(body.get("url", "")).strip())
    if not url:
        return JSONResponse({"error": "url required"}, status_code=400)
    label = str(body.get("label", "")).strip()
    ptype = str(body.get("type", "")).strip() or _detect_proxy_type(url)
    if not skip_test:
        ok, latency, err = await _test_proxy_url(url)
        if not ok:
            return {"ok": False, "error": err, "url": url}
        pid = await db.add_proxy(url, label, ptype, purpose=purpose)
        await db.update_proxy_test(pid, latency)
        return {"ok": True, "id": pid, "latency_ms": latency}
    pid = await db.add_proxy(url, label, ptype, purpose=purpose)
    return {"ok": True, "id": pid}


@router.delete("/proxy/{proxy_id}")
async def delete_proxy(proxy_id: int, _: bool = Depends(verify_admin)):
    ok = await db.delete_proxy(proxy_id)
    return {"ok": ok}


@router.post("/proxy/delete-failed")
async def delete_failed_proxies(request: Request, _: bool = Depends(verify_admin)):
    """Delete all proxies with failed tests (latency = -1) for a given purpose."""
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    proxies = await db.get_proxies(purpose=purpose)
    deleted = 0
    for p in proxies:
        if p.get("last_latency_ms", 0) == -1 and p.get("last_tested", ""):
            await db.delete_proxy(p["id"])
            deleted += 1
    return {"ok": True, "deleted": deleted}


@router.post("/proxy/edit-bulk")
async def edit_bulk_proxies(request: Request, _: bool = Depends(verify_admin)):
    """Replace all proxies for a purpose. Tests new/changed ones.

    Body: {urls: "line\\nline", purpose: "api"|"batch"}
    Deletes proxies not in the new list, adds new ones (with test), keeps unchanged.
    """
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    urls_raw = str(body.get("urls", "")).strip()
    new_lines = [_normalize_proxy_url(l.strip()) for l in urls_raw.splitlines() if l.strip()]

    existing = await db.get_proxies(purpose=purpose)
    existing_urls = {p["url"]: p for p in existing}
    new_url_set = set(new_lines)

    # Delete proxies not in new list
    deleted = 0
    for p in existing:
        if p["url"] not in new_url_set:
            await db.delete_proxy(p["id"])
            deleted += 1

    # Add new proxies (test first)
    added = []
    failed = []
    for url in new_lines:
        if url in existing_urls:
            continue  # Already exists, skip
        ptype = _detect_proxy_type(url)
        ok, latency, err = await _test_proxy_url(url)
        if ok:
            pid = await db.add_proxy(url, "", ptype, purpose=purpose)
            await db.update_proxy_test(pid, latency)
            added.append({"id": pid, "url": url, "type": ptype, "latency_ms": latency})
        else:
            failed.append({"url": url, "error": err})

    return {
        "ok": True,
        "deleted": deleted,
        "added": added,
        "failed": failed,
        "kept": len(new_lines) - len(added) - len(failed),
        "count_added": len(added),
        "count_failed": len(failed),
    }


@router.post("/proxy/test-purpose")
async def test_proxies_by_purpose(request: Request, _: bool = Depends(verify_admin)):
    """Test all proxies for a specific purpose."""
    import time as _time
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    proxies = await db.get_proxies(purpose=purpose)
    results = []
    for p in proxies:
        if not p.get("active", True):
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": "inactive", "latency_ms": -1})
            continue
        try:
            async with httpx.AsyncClient(proxy=p["url"], timeout=10) as client:
                t0 = _time.monotonic()
                resp = await client.get("https://httpbin.org/ip")
                latency = int((_time.monotonic() - t0) * 1000)
            await db.update_proxy_test(p["id"], latency)
            results.append({"id": p["id"], "url": p["url"], "ok": True, "latency_ms": latency})
        except Exception as e:
            await db.update_proxy_test(p["id"], -1, str(e)[:200])
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": str(e)[:200], "latency_ms": -1})
    return {"results": results}


@router.post("/proxy/delete-bulk")
async def delete_bulk_proxies(request: Request, _: bool = Depends(verify_admin)):
    """Delete multiple proxies by ID. Body: {ids: [1, 2, 3]}."""
    body = await request.json()
    ids = body.get("ids", [])
    deleted = 0
    for pid in ids:
        if await db.delete_proxy(int(pid)):
            deleted += 1
    return {"ok": True, "deleted": deleted}


@router.post("/proxy/{proxy_id}/toggle")
async def toggle_proxy(proxy_id: int, request: Request, _: bool = Depends(verify_admin)):
    body = await request.json()
    active = bool(body.get("active", True))
    ok = await db.toggle_proxy(proxy_id, active)

    from .proxy_kiro import close_all_clients as close_kiro
    from .proxy_codebuddy import close_all_clients as close_cb
    from .proxy_wavespeed import close_all_clients as close_ws
    await close_kiro()
    await close_cb()
    await close_ws()

    return {"ok": ok}


@router.post("/proxy/{proxy_id}/test")
async def test_single_proxy(proxy_id: int, _: bool = Depends(verify_admin)):
    """Test a single proxy by connecting to httpbin."""
    proxies = await db.get_proxies()
    proxy = next((p for p in proxies if p["id"] == proxy_id), None)
    if not proxy:
        return JSONResponse({"error": "Proxy not found"}, status_code=404)

    import time as _time
    try:
        async with httpx.AsyncClient(proxy=proxy["url"], timeout=10) as client:
            t0 = _time.monotonic()
            resp = await client.get("https://httpbin.org/ip")
            latency = int((_time.monotonic() - t0) * 1000)
        await db.update_proxy_test(proxy_id, latency)
        return {"ok": True, "latency_ms": latency, "status": resp.status_code, "ip": resp.json().get("origin", "")}
    except Exception as e:
        await db.update_proxy_test(proxy_id, -1, str(e)[:200])
        return {"ok": False, "error": str(e)[:200]}


@router.post("/proxy/test-all")
async def test_all_proxies(_: bool = Depends(verify_admin)):
    """Test all active proxies."""
    import time as _time
    proxies = await db.get_proxies()
    results = []
    for p in proxies:
        if not p["active"]:
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": "inactive", "latency_ms": -1})
            continue
        try:
            async with httpx.AsyncClient(proxy=p["url"], timeout=10) as client:
                t0 = _time.monotonic()
                resp = await client.get("https://httpbin.org/ip")
                latency = int((_time.monotonic() - t0) * 1000)
            await db.update_proxy_test(p["id"], latency)
            results.append({"id": p["id"], "url": p["url"], "ok": True, "latency_ms": latency})
        except Exception as e:
            await db.update_proxy_test(p["id"], -1, str(e)[:200])
            results.append({"id": p["id"], "url": p["url"], "ok": False, "error": str(e)[:200], "latency_ms": -1})
    return {"results": results}


@router.post("/proxy/{proxy_id}/check")
async def check_proxy(proxy_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Toggle checked state of a proxy. Body: {checked: bool, purpose?: str}."""
    body = await request.json()
    checked = bool(body.get("checked", True))
    purpose = str(body.get("purpose", "api")).strip()
    await db.toggle_proxy_checked(proxy_id, checked, purpose)
    return {"ok": True}


@router.post("/proxy/smart-rotate")
async def set_smart_rotate(request: Request, _: bool = Depends(verify_admin)):
    """Enable/disable smart-rotate for a purpose. Body: {purpose, enabled}."""
    body = await request.json()
    purpose = str(body.get("purpose", "api")).strip()
    enabled = bool(body.get("enabled", True))

    if purpose == "batch":
        await db.set_setting("batch_smart_rotate", "true" if enabled else "false")
    else:
        await db.set_setting("api_proxy_smart_rotate", "true" if enabled else "false")

    # Just save the mode — don't touch checkbox state
    return {"ok": True, "smart_rotate": enabled}


@router.post("/proxy/batch-toggle")
async def batch_proxy_toggle(request: Request, _: bool = Depends(verify_admin)):
    """Enable/disable the batch proxy pool. Body: {enabled: bool}."""
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    await db.set_setting("batch_proxy_enabled", "true" if enabled else "false")
    return {"ok": True, "enabled": enabled}


# ---------------------------------------------------------------------------
# Account verification (temporary → fix)
# ---------------------------------------------------------------------------

@router.post("/accounts/test-batch")
async def test_batch_accounts(request: Request, _: bool = Depends(verify_admin)):
    """Test all temporary exhausted/banned accounts with a small request.

    Sends a tiny prompt to each provider. If error → show error log for review.
    Uses smart proxy rotation if enabled. 2s delay between each test.
    """
    import time as _time

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    providers_filter = body.get("providers", ["kiro", "codebuddy", "wavespeed", "chatbai", "skillboss", "windsurf"])
    account_ids_filter = body.get("account_ids", [])

    # Normalize provider aliases (dashboard sends "cbai" but loop uses "chatbai")
    _provider_aliases = {"cbai": "chatbai", "skboss": "skillboss", "cb": "codebuddy", "ws": "wavespeed", "gl": "gumloop"}
    providers_filter = [_provider_aliases.get(p, p) for p in providers_filter]

    accounts = await db.get_accounts()
    # Filter by account_ids if specified
    if account_ids_filter:
        id_set = set(int(i) for i in account_ids_filter)
        accounts = [a for a in accounts if a["id"] in id_set]
    results = []
    tested_count = 0

    for acc in accounts:
        acct_id = acc["id"]
        email = acc["email"]

        # Test each provider that is exhausted/banned and NOT yet verified
        for provider, status_col, verified_col, test_err_col in [
            ("kiro", "kiro_status", "kiro_verified", "kiro_test_error"),
            ("codebuddy", "cb_status", "cb_verified", "cb_test_error"),
            ("wavespeed", "ws_status", "ws_verified", "ws_test_error"),
            ("gumloop", "gl_status", "gl_verified", "gl_test_error"),
            ("chatbai", "cbai_status", "cbai_verified", "cbai_test_error"),
            ("skillboss", "skboss_status", "skboss_verified", "skboss_test_error"),
            ("windsurf", "windsurf_status", "windsurf_verified", "windsurf_test_error"),
        ]:
            if provider not in providers_filter:
                continue
            status = acc.get(status_col, "")
            verified = acc.get(verified_col, 0)
            if status not in ("exhausted", "banned", "failed", "rate_limited") or verified == 1:
                continue

            # Delay between tests (2s base, skip first)
            tested_count += 1
            if tested_count > 1:
                await asyncio.sleep(2)

            # Get proxy for this test (smart rotate)
            proxy_url = await db.get_next_proxy_url()

            # Send test request
            test_result = await _test_account_provider(acc, provider, proxy_url)
            error_msg = test_result.get("error", "")

            # If 400/rate limit, back off extra 5s before next test
            if "400" in str(test_result.get("latency_ms", "")) or "HTTP 400" in error_msg or "rate" in error_msg.lower() or "limit" in error_msg.lower():
                await asyncio.sleep(5)

            # Store test error for review
            await db.update_account(acct_id, **{test_err_col: error_msg})

            # Auto-restore on success: set status back to ok, clear errors
            auto_approved = False
            if test_result["ok"]:
                restore_fields = {status_col: "ok", verified_col: 0, test_err_col: ""}
                if provider == "kiro":
                    restore_fields.update(kiro_error="", kiro_error_count=0)
                elif provider == "codebuddy":
                    restore_fields.update(cb_error="", cb_error_count=0)
                elif provider == "wavespeed":
                    restore_fields.update(ws_error="", ws_error_count=0)
                elif provider == "gumloop":
                    restore_fields.update(gl_error="", gl_error_count=0)
                elif provider in ("chatbai", "cbai"):
                    restore_fields.update(cbai_error="", cbai_error_count=0)
                elif provider in ("skillboss", "skboss"):
                    restore_fields.update(skboss_error="", skboss_error_count=0)
                await db.update_account(acct_id, **restore_fields)
                # Push to D1 so it doesn't get overwritten
                try:
                    await license_client.push_account_now(await db.get_account(acct_id))
                except Exception:
                    pass
            else:
                # Auto-approve (mark as verified fix) if response clearly indicates exhausted/banned
                exhaust_signals = [
                    "credits exhausted", "credit exhausted", "quota exceeded",
                    "code: 14018", "14018", "account suspended", "account banned",
                    "account disabled", "no remaining", "limit reached",
                ]
                error_lower = error_msg.lower()
                if any(sig in error_lower for sig in exhaust_signals):
                    await db.update_account(acct_id, **{verified_col: 1, test_err_col: error_msg})
                    auto_approved = True

            results.append({
                "account_id": acct_id,
                "email": email,
                "provider": provider,
                "status": status,
                "test_ok": test_result["ok"],
                "restored": test_result["ok"],
                "auto_approved": auto_approved,
                "error": error_msg,
                "proxy_used": proxy_url or "direct",
                "latency_ms": test_result.get("latency_ms", 0),
            })

    restored = sum(1 for r in results if r.get("restored"))
    return {"results": results, "total": len(results), "tested": tested_count, "restored": restored}


async def _test_account_provider(acc: dict, provider: str, proxy_url: str | None) -> dict:
    """Test account by calling the provider proxy directly (bypasses routing).

    Calls the provider-specific proxy function with the exact account,
    then logs the result to usage_logs so it appears in the Logs tab.
    """
    import time as _time

    acct_id = acc["id"]
    email = acc.get("email", "?")

    if provider == "codebuddy":
        cb_key = acc.get("cb_api_key", "")
        if not cb_key:
            return {"ok": False, "error": "No CB API key"}
        from .proxy_codebuddy import proxy_chat_completions as cb_proxy
        import random as _rnd
        _cb_models = ["claude-opus-4.6", "gpt-5.4", "gpt-5.2", "gemini-2.5-pro", "deepseek-v3-2-volc"]
        test_model = _rnd.choice(_cb_models)
        body = {"model": test_model, "messages": [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say OK"}], "max_tokens": 100, "stream": False}
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            response, credit = await cb_proxy(body, cb_key, False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            # Log it
            resp_body = ""
            if hasattr(response, "body"):
                try:
                    resp_body = response.body.decode("utf-8", errors="replace")[:2000]
                except Exception:
                    pass
            await db.log_usage(None, acct_id, test_model, "max", status, latency,
                               request_body=json.dumps(body), response_body=resp_body, proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            error = f"HTTP {status}"
            try:
                err_data = json.loads(resp_body)
                error = err_data.get("error", {}).get("message", error) if isinstance(err_data, dict) else error
            except Exception:
                pass
            return {"ok": False, "error": error[:300], "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            await db.log_usage(None, acct_id, test_model, "max", 502, latency,
                               request_body=json.dumps(body), error_message=str(e)[:200], proxy_url=px or "")
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "kiro":
        if not acc.get("kiro_access_token"):
            return {"ok": False, "error": "No kiro access token"}
        from .proxy_kiro import proxy_chat_completions as kiro_proxy
        from fastapi import Request as _Req
        body = json.dumps({"model": "auto", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 16, "stream": False}).encode()
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            # kiro_proxy needs a Request object — create a minimal mock
            response = await kiro_proxy(None, body, account=acc, is_stream=False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            await db.log_usage(None, acct_id, "auto", "standard", status, latency,
                               request_body='{"test":"batch"}', proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"HTTP {status}", "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "wavespeed":
        ws_key = acc.get("ws_api_key", "")
        if not ws_key:
            return {"ok": False, "error": "No WS API key"}
        from .proxy_wavespeed import proxy_chat_completions as ws_proxy
        body = {"model": "new-claude-sonnet-4", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 16, "stream": False}
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            response, cost = await ws_proxy(body, ws_key, False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            await db.log_usage(None, acct_id, "new-claude-sonnet-4", "wavespeed", status, latency,
                               request_body='{"test":"batch"}', proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"HTTP {status}", "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "gumloop":
        if not acc.get("gl_refresh_token") or not acc.get("gl_gummie_id"):
            return {"ok": False, "error": "No GL credentials"}
        from .proxy_gumloop import proxy_chat_completions as gl_proxy
        body = {"model": "gl-claude-sonnet-4-5", "messages": [{"role": "user", "content": "Say OK"}], "max_tokens": 50, "stream": False}
        t0 = _time.monotonic()
        try:
            response, cost = await gl_proxy(body, acc, False)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            await db.log_usage(None, acct_id, "gl-claude-sonnet-4-5", "max_gl", status, latency,
                               request_body='{"test":"batch"}')
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            return {"ok": False, "error": f"HTTP {status}", "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "cbai" or provider == "chatbai":
        cbai_key = acc.get("cbai_api_key", "")
        if not cbai_key:
            return {"ok": False, "error": "No ChatBAI API key"}
        from .chatbai.proxy import proxy_chat_completions as cbai_proxy
        import random as _rnd
        _cbai_models = ["glm-5", "bchatai-kimi-k2.5", "bchatai-gpt-5-mini", "bchatai-minimax-m2.5", "bchatai-gpt-5-nano", "bchatai-gpt-5.2", "bchatai-deepseek-v4-flash", "bchatai-deepseek-v4-pro"]
        test_model = _rnd.choice(_cbai_models)
        body = {"model": test_model, "messages": [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say OK"}], "max_tokens": 16, "stream": False}
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            response, cost = await cbai_proxy(body, cbai_key, False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_body = ""
            if hasattr(response, "body"):
                try:
                    resp_body = response.body.decode("utf-8", errors="replace")[:2000]
                except Exception:
                    pass
            await db.log_usage(None, acct_id, test_model, "chatbai", status, latency,
                               request_body=json.dumps(body), response_body=resp_body, proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            error = f"HTTP {status}"
            try:
                err_data = json.loads(resp_body)
                error = err_data.get("error", {}).get("message", error) if isinstance(err_data, dict) else error
            except Exception:
                pass
            return {"ok": False, "error": error[:300], "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            await db.log_usage(None, acct_id, test_model, "chatbai", 502, latency,
                               request_body=json.dumps(body), error_message=str(e)[:200], proxy_url=px or "")
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "skillboss" or provider == "skboss":
        skboss_key = acc.get("skboss_api_key", "")
        if not skboss_key:
            return {"ok": False, "error": "No SkillBoss API key"}
        from .proxy_skillboss import proxy_chat_completions as skboss_proxy
        import random as _rnd
        _skb_models = ["skboss-claude-haiku-4.5", "skboss-gpt-5.2", "skboss-gemini-2.5-flash"]
        test_model = _rnd.choice(_skb_models)
        body = {"model": test_model, "messages": [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say OK"}], "max_tokens": 50, "stream": False}
        proxy_info = await db.get_proxy_for_api_call()
        px = proxy_info["url"] if proxy_info else None
        t0 = _time.monotonic()
        try:
            response, cost = await skboss_proxy(body, skboss_key, False, proxy_url=px)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_body = ""
            if hasattr(response, "body"):
                try:
                    resp_body = response.body.decode("utf-8", errors="replace")[:2000]
                except Exception:
                    pass
            await db.log_usage(None, acct_id, test_model, "skillboss", status, latency,
                               request_body=json.dumps(body), response_body=resp_body, proxy_url=px or "")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            error = f"HTTP {status}"
            try:
                err_data = json.loads(resp_body)
                error = err_data.get("error", {}).get("message", error) if isinstance(err_data, dict) else error
            except Exception:
                pass
            return {"ok": False, "error": error[:300], "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            await db.log_usage(None, acct_id, test_model, "skillboss", 502, latency,
                               request_body=json.dumps(body), error_message=str(e)[:200], proxy_url=px or "")
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    elif provider == "windsurf":
        windsurf_key = acc.get("windsurf_api_key", "")
        if not windsurf_key:
            return {"ok": False, "error": "No Windsurf API key"}
        from .proxy_windsurf import proxy_chat_completions as wf_proxy
        test_model = "windsurf-claude-sonnet-4.6"
        body = {"model": test_model, "messages": [{"role": "system", "content": "You are helpful."}, {"role": "user", "content": "Say OK"}], "max_tokens": 50, "stream": False}
        t0 = _time.monotonic()
        try:
            response, cost = await wf_proxy(body, windsurf_key, False)
            latency = int((_time.monotonic() - t0) * 1000)
            status = response.status_code if hasattr(response, "status_code") else 200
            resp_body = ""
            if hasattr(response, "body"):
                try:
                    resp_body = response.body.decode("utf-8", errors="replace")[:2000]
                except Exception:
                    pass
            await db.log_usage(None, acct_id, test_model, "windsurf", status, latency,
                               request_body=json.dumps(body), response_body=resp_body, proxy_url="")
            if status == 200:
                return {"ok": True, "latency_ms": latency}
            error = f"HTTP {status}"
            try:
                err_data = json.loads(resp_body)
                error = err_data.get("error", {}).get("message", error) if isinstance(err_data, dict) else error
            except Exception:
                pass
            return {"ok": False, "error": error[:300], "latency_ms": latency}
        except Exception as e:
            latency = int((_time.monotonic() - t0) * 1000)
            await db.log_usage(None, acct_id, test_model, "windsurf", 502, latency,
                               request_body=json.dumps(body), error_message=str(e)[:200], proxy_url="")
            return {"ok": False, "error": str(e)[:300], "latency_ms": latency}

    return {"ok": False, "error": f"Unknown provider: {provider}"}


@router.post("/accounts/{account_id}/approve/{provider}")
async def approve_account(account_id: int, provider: str, _: bool = Depends(verify_admin)):
    """Approve a temporary exhausted/banned account as fix (confirmed dead)."""
    col_map = {
        "kiro": "kiro_verified",
        "codebuddy": "cb_verified",
        "cb": "cb_verified",
        "wavespeed": "ws_verified",
        "ws": "ws_verified",
        "gumloop": "gl_verified",
        "gl": "gl_verified",
        "chatbai": "cbai_verified",
        "cbai": "cbai_verified",
        "skillboss": "skboss_verified",
        "skboss": "skboss_verified",
        "windsurf": "windsurf_verified",
    }
    col = col_map.get(provider)
    if not col:
        return JSONResponse({"error": f"Unknown provider: {provider}"}, status_code=400)
    await db.update_account(account_id, **{col: 1})
    return {"ok": True}


_warmup_cancel = False

@router.post("/accounts/warmup/cancel")
async def cancel_warmup(_: bool = Depends(verify_admin)):
    """Cancel running warmup."""
    global _warmup_cancel
    _warmup_cancel = True
    return {"ok": True, "message": "Warmup cancel requested"}


@router.post("/accounts/warmup")
async def warmup_accounts(request: Request, _: bool = Depends(verify_admin)):
    """Warmup: re-test banned/exhausted accounts in batches of 8, 30s pause between batches.

    Supports cancellation via POST /accounts/warmup/cancel.
    """
    global _warmup_cancel
    _warmup_cancel = False

    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    provider_filter = body.get("provider")

    accounts = await db.get_accounts()

    targets = []
    provider_configs = [
        ("codebuddy", "cb_status", "cb_api_key", "cb_error"),
        ("chatbai", "cbai_status", "cbai_api_key", "cbai_error"),
        ("skillboss", "skboss_status", "skboss_api_key", "skboss_error"),
        ("kiro", "kiro_status", "kiro_access_token", "kiro_error"),
        ("wavespeed", "ws_status", "ws_api_key", "ws_error"),
        ("gumloop", "gl_status", "gl_refresh_token", "gl_error"),
        ("windsurf", "windsurf_status", "windsurf_api_key", "windsurf_error"),
    ]

    for acc in accounts:
        if acc.get("status") == "trash":
            continue
        for prov, status_col, key_col, error_col in provider_configs:
            if provider_filter and prov != provider_filter:
                continue
            status = acc.get(status_col, "none")
            has_key = bool(acc.get(key_col, ""))
            if status in ("banned", "exhausted", "failed", "rate_limited") and has_key:
                targets.append((acc, prov, status_col, error_col))

    if not targets:
        return {"ok": True, "tested": 0, "restored": 0, "total_targets": 0, "message": "No accounts to warmup"}

    restored = 0
    tested = 0
    results = []
    total_targets = len(targets)
    batch_size = 8
    cancelled = False

    for acc, prov, status_col, error_col in targets:
        # Check cancel
        if _warmup_cancel:
            cancelled = True
            break

        tested += 1

        # After every 8 accounts, pause 10s; otherwise 2s between each
        if tested > 1 and (tested - 1) % batch_size == 0:
            await asyncio.sleep(10)
        elif tested > 1:
            await asyncio.sleep(2)

        # Check cancel again after sleep
        if _warmup_cancel:
            cancelled = True
            break

        proxy_info = await db.get_proxy_for_api_call()
        proxy_url = proxy_info["url"] if proxy_info else None

        test_result = await _test_account_provider(acc, prov, proxy_url)

        if test_result.get("ok"):
            # Restore account — clear status, error, and error count
            error_count_col = status_col.replace("_status", "_error_count")
            await db.update_account(acc["id"], **{status_col: "ok", error_col: "", error_count_col: 0})
            # Push to D1 so it doesn't get overwritten on next sync
            try:
                await license_client.push_account_now(await db.get_account(acc["id"]))
            except Exception:
                pass
            restored += 1
            results.append({"email": acc["email"], "provider": prov, "status": "restored"})
        else:
            results.append({"email": acc["email"], "provider": prov, "status": "still_dead", "error": test_result.get("error", "")[:100]})

        # 2s delay between tests
        if tested < len(targets):
            await asyncio.sleep(2)

    return {"ok": True, "tested": tested, "restored": restored, "total_targets": total_targets, "cancelled": cancelled, "results": results}


@router.post("/accounts/approve-all")
async def approve_all_accounts(request: Request, _: bool = Depends(verify_admin)):
    """Bulk approve all temporary exhausted/banned as fix."""
    body = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    provider = body.get("provider")  # optional: filter by provider

    accounts = await db.get_accounts()
    approved = 0
    for acc in accounts:
        for prov, status_col, verified_col in [
            ("kiro", "kiro_status", "kiro_verified"),
            ("codebuddy", "cb_status", "cb_verified"),
            ("wavespeed", "ws_status", "ws_verified"),
            ("gumloop", "gl_status", "gl_verified"),
            ("cbai", "cbai_status", "cbai_verified"),
            ("skillboss", "skboss_status", "skboss_verified"),
            ("windsurf", "windsurf_status", "windsurf_verified"),
        ]:
            if provider and prov != provider:
                continue
            status = acc.get(status_col, "")
            verified = acc.get(verified_col, 0)
            if status in ("exhausted", "banned", "failed", "rate_limited") and verified == 0:
                await db.update_account(acc["id"], **{verified_col: 1})
                approved += 1

    return {"ok": True, "approved": approved}


# ---------------------------------------------------------------------------
# Filter rules
# ---------------------------------------------------------------------------

@router.get("/global-filters")
async def list_global_filters(_: bool = Depends(verify_admin)):
    """List global filter rules from central admin (cached from last sync)."""
    from . import license_client
    # Return cached global filters from last sync pull
    gf = getattr(license_client, '_global_filters', [])
    return {"global_filters": gf, "count": len(gf)}


@router.get("/filters")
async def list_filters(_: bool = Depends(verify_admin)):
    """List all filter rules."""
    filters = await db.get_filters()
    return {"filters": filters, "count": len(filters)}


@router.post("/filters")
async def create_filter_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Create a new filter rule."""
    body = await request.json()
    find_text = str(body.get("find_text", "")).strip()
    if not find_text:
        return JSONResponse({"error": "find_text is required"}, status_code=400)
    replace_text = str(body.get("replace_text", ""))
    is_regex = bool(body.get("is_regex", False))
    description = str(body.get("description", "")).strip()

    if is_regex:
        try:
            re.compile(find_text)
        except re.error as e:
            return JSONResponse({"error": f"Invalid regex: {e}"}, status_code=400)

    filter_id = await db.create_filter(find_text, replace_text, is_regex, description)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True, "id": filter_id}


@router.put("/filters/{filter_id}")
async def update_filter_endpoint(filter_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Update a filter rule."""
    body = await request.json()
    fields: dict = {}
    if "find_text" in body:
        fields["find_text"] = str(body["find_text"]).strip()
    if "replace_text" in body:
        fields["replace_text"] = str(body["replace_text"])
    if "is_regex" in body:
        fields["is_regex"] = 1 if body["is_regex"] else 0
    if "enabled" in body:
        fields["enabled"] = 1 if body["enabled"] else 0
    if "description" in body:
        fields["description"] = str(body["description"]).strip()

    if not fields:
        return JSONResponse({"error": "No fields to update"}, status_code=400)

    if fields.get("is_regex") and "find_text" in fields:
        try:
            re.compile(fields["find_text"])
        except re.error as e:
            return JSONResponse({"error": f"Invalid regex: {e}"}, status_code=400)

    ok = await db.update_filter(filter_id, **fields)
    if not ok:
        return JSONResponse({"error": "Filter not found"}, status_code=404)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True}


@router.delete("/filters/{filter_id}")
async def delete_filter_endpoint(filter_id: int, _: bool = Depends(verify_admin)):
    """Delete a filter rule."""
    ok = await db.delete_filter(filter_id)
    if not ok:
        return JSONResponse({"error": "Filter not found"}, status_code=404)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True}


@router.post("/filters/{filter_id}/toggle")
async def toggle_filter_endpoint(filter_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Toggle a filter rule on/off."""
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    ok = await db.toggle_filter(filter_id, enabled)
    if not ok:
        return JSONResponse({"error": "Filter not found"}, status_code=404)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True}


@router.post("/filters/seed")
async def seed_filters_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Seed default filter rules. Send {force: true} to replace all existing rules."""
    force = False
    try:
        body = await request.json()
        force = bool(body.get("force", False))
    except Exception:
        pass
    count = await db.seed_default_filters(force=force)
    from .message_filter import invalidate_cache
    invalidate_cache()
    return {"ok": True, "seeded": count}


# ---------------------------------------------------------------------------
# Tunnel Management
# ---------------------------------------------------------------------------

@router.get("/tunnel/status")
async def tunnel_status(request: Request, target: str = "", _: bool = Depends(verify_admin)):
    """Get tunnel status. ?target=proxy|mcp or omit for all."""
    from .tunnel_manager import get_tunnel_status, get_system_info
    if target:
        status = get_tunnel_status(target)
    else:
        status = get_tunnel_status()
    sys_info = get_system_info()
    return {"tunnels": status, "system": sys_info}


@router.post("/tunnel/start")
async def tunnel_start(request: Request, _: bool = Depends(verify_admin)):
    """Start a cloudflared tunnel. Body: {target: "proxy"|"mcp", port?: int}."""
    from .tunnel_manager import start_tunnel
    body = await request.json()
    target = str(body.get("target", "proxy")).strip()
    port = body.get("port")
    if target not in ("proxy", "mcp"):
        return JSONResponse({"error": "target must be 'proxy' or 'mcp'"}, status_code=400)
    result = start_tunnel(target, port)
    return result  # Always 200 — check result.ok in frontend


@router.post("/tunnel/stop")
async def tunnel_stop(request: Request, _: bool = Depends(verify_admin)):
    """Stop a cloudflared tunnel. Body: {target: "proxy"|"mcp"}."""
    from .tunnel_manager import stop_tunnel
    body = await request.json()
    target = str(body.get("target", "proxy")).strip()
    if target not in ("proxy", "mcp"):
        return JSONResponse({"error": "target must be 'proxy' or 'mcp'"}, status_code=400)
    result = stop_tunnel(target)
    return result


@router.post("/tunnel/install-cloudflared")
async def install_cloudflared_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Install cloudflared on this server (Linux only)."""
    from .tunnel_manager import install_cloudflared
    result = await install_cloudflared()
    return result  # Always 200 — check result.ok in frontend


@router.post("/tunnel/install-nginx")
async def install_nginx_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Install nginx on this server (Linux only)."""
    from .tunnel_manager import install_nginx
    result = await install_nginx()
    return result  # Always 200 — check result.ok in frontend


@router.post("/tunnel/nginx-config")
async def generate_nginx_config_endpoint(request: Request, _: bool = Depends(verify_admin)):
    """Generate nginx config. Body: {mode, domain?, server_ip?, proxy_port?, mcp_port?, enable_ssl?, ssl_email?}."""
    from .tunnel_manager import generate_nginx_config
    body = await request.json()
    result = generate_nginx_config(
        mode=str(body.get("mode", "")).strip(),
        domain=str(body.get("domain", "")).strip(),
        server_ip=str(body.get("server_ip", "")).strip(),
        proxy_port=int(body.get("proxy_port", LISTEN_PORT)),
        mcp_port=int(body.get("mcp_port", 9876)),
        enable_ssl=bool(body.get("enable_ssl", False)),
        ssl_email=str(body.get("ssl_email", "")).strip(),
    )
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return result


@router.get("/tunnel/detect-ip")
async def detect_ip_endpoint(_: bool = Depends(verify_admin)):
    """Detect the public IP of this VPS."""
    from .tunnel_manager import detect_vps_ip
    ip = await detect_vps_ip()
    return {"ip": ip}


# ---------------------------------------------------------------------------
# MCP Server Instances (multi-instance management)
# ---------------------------------------------------------------------------

@router.get("/mcp/apikey")
async def get_mcp_apikey(_: bool = Depends(verify_admin)):
    """Get the current MCP API key."""
    api_key_file = _Path(__file__).resolve().parent / "data" / ".mcp_api_key"
    key = ""
    if api_key_file.exists():
        key = api_key_file.read_text().strip()
    return {"key": key, "path": str(api_key_file)}


@router.post("/mcp/apikey")
async def set_mcp_apikey(request: Request, _: bool = Depends(verify_admin)):
    """Set the MCP API key. Body: {key: "sk-xxx"}. Saves to file for MCP servers to use."""
    body = await request.json()
    key = str(body.get("key", "")).strip()
    if not key:
        return JSONResponse({"error": "key is required"}, status_code=400)
    api_key_file = _Path(__file__).resolve().parent / "data" / ".mcp_api_key"
    api_key_file.parent.mkdir(parents=True, exist_ok=True)
    api_key_file.write_text(key)
    return {"ok": True, "message": "API key saved. Restart MCP servers to apply."}


@router.get("/mcp/instances")
async def list_mcp_instances(_: bool = Depends(verify_admin)):
    """List all MCP server instances with live status check."""
    instances = await db.get_mcp_instances()
    # Check if PIDs are still alive
    import os as _os
    for inst in instances:
        pid = inst.get("pid", 0)
        if pid and not _pid_alive(pid):
            await db.update_mcp_instance(inst["id"], pid=0, status="stopped")
            inst["pid"] = 0
            inst["status"] = "stopped"
        tpid = inst.get("tunnel_pid", 0)
        if tpid and not _pid_alive(tpid):
            await db.update_mcp_instance(inst["id"], tunnel_pid=0, tunnel_url="")
            inst["tunnel_pid"] = 0
            inst["tunnel_url"] = ""
    return {"instances": instances, "count": len(instances)}


@router.post("/mcp/instances")
async def add_mcp_instance(request: Request, _: bool = Depends(verify_admin)):
    """Add a new MCP server instance. Body: {workspace_path, port?}.

    If port=0 or not provided, auto-assigns next available port starting from 9876.
    """
    body = await request.json()
    workspace_path = str(body.get("workspace_path", "")).strip()
    port = int(body.get("port", 0))
    if not workspace_path:
        return JSONResponse({"error": "workspace_path is required"}, status_code=400)

    p = _Path(workspace_path).expanduser().resolve()
    if not p.exists():
        return JSONResponse({"error": f"Path does not exist: {workspace_path}"}, status_code=400)
    if not p.is_dir():
        return JSONResponse({"error": f"Not a directory: {workspace_path}"}, status_code=400)

    # Check duplicate path
    existing = await db.get_mcp_instance_by_path(str(p))
    if existing:
        return JSONResponse({"error": f"MCP server already exists for this path (ID {existing['id']})"}, status_code=409)

    # Auto-assign port if 0
    if port <= 0:
        instances = await db.get_mcp_instances()
        used_ports = {inst["port"] for inst in instances}
        port = 9876
        while port in used_ports:
            port += 1

    mcp_id = await db.add_mcp_instance(str(p), port)
    return {"ok": True, "id": mcp_id, "workspace_path": str(p), "port": port}


@router.delete("/mcp/instances/{mcp_id}")
async def remove_mcp_instance(mcp_id: int, _: bool = Depends(verify_admin)):
    """Remove an MCP instance (stops it first if running)."""
    inst = await db.get_mcp_instance(mcp_id)
    if not inst:
        return JSONResponse({"error": "Not found"}, status_code=404)
    # Stop if running
    if inst.get("pid") and _pid_alive(inst["pid"]):
        _kill_pid_safe(inst["pid"])
    if inst.get("tunnel_pid") and _pid_alive(inst["tunnel_pid"]):
        _kill_pid_safe(inst["tunnel_pid"])
    await db.delete_mcp_instance(mcp_id)
    return {"ok": True}


@router.post("/mcp/instances/{mcp_id}/start")
async def start_mcp_instance(mcp_id: int, _: bool = Depends(verify_admin)):
    """Start an MCP server instance."""
    import subprocess as _sp
    inst = await db.get_mcp_instance(mcp_id)
    if not inst:
        return JSONResponse({"error": "Not found"}, status_code=404)

    # Validate workspace path exists on THIS machine
    ws = _Path(inst["workspace_path"])
    if not ws.exists():
        return JSONResponse({
            "error": f"Workspace path does not exist on this machine: {inst['workspace_path']}. "
                     f"Remove this instance and add a new one with a valid path."
        }, status_code=400)

    # Already running?
    if inst.get("pid") and _pid_alive(inst["pid"]):
        return {"ok": True, "pid": inst["pid"], "message": "Already running"}

    install_dir = _Path(__file__).resolve().parent.parent
    python_bin = install_dir / ".venv" / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")
    if not python_bin.exists():
        import sys
        python_bin = _Path(sys.executable)
    mcp_script = install_dir / "mcp_server.py"

    log_file = _Path(inst["workspace_path"]) / ".mcp_server.log"
    cmd = [
        str(python_bin), str(mcp_script),
        "--workspace", inst["workspace_path"],
        "--port", str(inst["port"]),
        "--no-tunnel", "--no-interactive",
    ]
    # Load API key — from file first, then generate from DB if needed
    api_key = ""
    api_key_file = install_dir / "unified" / "data" / ".mcp_api_key"
    if api_key_file.exists():
        api_key = api_key_file.read_text().strip()
    if not api_key:
        # Try to get first active API key from DB and regenerate a temp one
        keys = await db.get_api_keys()
        if keys:
            # We can't recover the full key from hash, so generate a new one for MCP
            _, new_key = await db.create_api_key("mcp-auto")
            api_key = new_key
            # Save for future MCP starts
            api_key_file.parent.mkdir(parents=True, exist_ok=True)
            api_key_file.write_text(api_key)
    if api_key:
        cmd.extend(["--api-key", api_key])

    log_fh = open(log_file, "a")
    if os.name == "nt":
        proc = _sp.Popen(cmd, stdout=log_fh, stderr=_sp.STDOUT,
                         creationflags=_sp.CREATE_NO_WINDOW | _sp.DETACHED_PROCESS, close_fds=True)
    else:
        proc = _sp.Popen(cmd, stdout=log_fh, stderr=_sp.STDOUT,
                         start_new_session=True, close_fds=True)
    log_fh.close()  # Parent doesn't need the handle — child inherited it

    await db.update_mcp_instance(mcp_id, pid=proc.pid, status="running")
    return {"ok": True, "pid": proc.pid, "port": inst["port"]}


@router.post("/mcp/instances/{mcp_id}/stop")
async def stop_mcp_instance(mcp_id: int, _: bool = Depends(verify_admin)):
    """Stop an MCP server instance."""
    inst = await db.get_mcp_instance(mcp_id)
    if not inst:
        return JSONResponse({"error": "Not found"}, status_code=404)
    pid = inst.get("pid", 0)
    if pid and _pid_alive(pid):
        _kill_pid_safe(pid)
    # Also stop tunnel if running
    tpid = inst.get("tunnel_pid", 0)
    if tpid and _pid_alive(tpid):
        _kill_pid_safe(tpid)
    await db.update_mcp_instance(mcp_id, pid=0, status="stopped", tunnel_pid=0, tunnel_url="")
    return {"ok": True}


@router.post("/mcp/instances/{mcp_id}/tunnel")
async def toggle_mcp_tunnel(mcp_id: int, request: Request, _: bool = Depends(verify_admin)):
    """Start or stop cloudflared tunnel for an MCP instance. Body: {action: "start"|"stop"}."""
    body = await request.json()
    action = str(body.get("action", "start")).strip()
    inst = await db.get_mcp_instance(mcp_id)
    if not inst:
        return JSONResponse({"error": "Not found"}, status_code=404)

    if action == "stop":
        tpid = inst.get("tunnel_pid", 0)
        if tpid and _pid_alive(tpid):
            _kill_pid_safe(tpid)
        await db.update_mcp_instance(mcp_id, tunnel_pid=0, tunnel_url="")
        return {"ok": True, "message": "Tunnel stopped"}

    # Start tunnel
    from .tunnel_manager import check_cloudflared
    import subprocess as _sp
    cf_path = check_cloudflared()
    if not cf_path:
        return {"ok": False, "error": "cloudflared not installed"}

    port = inst["port"]
    state_dir = _Path(__file__).resolve().parent / "data" / "tunnels"
    state_dir.mkdir(parents=True, exist_ok=True)
    log_path = state_dir / f"mcp_{mcp_id}.log"

    log_fh = open(log_path, "w")
    if os.name == "nt":
        proc = _sp.Popen([cf_path, "tunnel", "--url", f"http://localhost:{port}"],
                         stdout=_sp.DEVNULL, stderr=log_fh,
                         creationflags=_sp.CREATE_NO_WINDOW | _sp.DETACHED_PROCESS)
    else:
        proc = _sp.Popen([cf_path, "tunnel", "--url", f"http://localhost:{port}"],
                         stdout=_sp.DEVNULL, stderr=log_fh,
                         start_new_session=True, close_fds=True)
    log_fh.close()  # Child inherited the fd — parent can release

    # Poll log for URL
    import time as _time, re as _re
    tunnel_url = ""
    for _ in range(50):
        _time.sleep(0.5)
        if proc.poll() is not None:
            break
        try:
            content = log_path.read_text(errors="replace")
            match = _re.search(r'(https://[a-z0-9\-]+\.trycloudflare\.com)', content)
            if match:
                tunnel_url = match.group(1)
                break
        except Exception:
            pass

    await db.update_mcp_instance(mcp_id, tunnel_pid=proc.pid, tunnel_url=tunnel_url)
    if tunnel_url:
        return {"ok": True, "url": tunnel_url, "pid": proc.pid}
    return {"ok": False, "error": "Timeout waiting for tunnel URL", "pid": proc.pid}


@router.get("/mcp/instances/by-path")
async def get_mcp_by_path(path: str, _: bool = Depends(verify_admin)):
    """Check if a path has an MCP instance. Used by explorer."""
    resolved = str(_Path(path).expanduser().resolve())
    inst = await db.get_mcp_instance_by_path(resolved)
    if inst:
        # Live check
        if inst.get("pid") and not _pid_alive(inst["pid"]):
            await db.update_mcp_instance(inst["id"], pid=0, status="stopped")
            inst["pid"] = 0
            inst["status"] = "stopped"
        return {"found": True, "instance": inst}
    return {"found": False}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            import subprocess as _sp
            r = _sp.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, timeout=5)
            return str(pid) in r.stdout
        else:
            os.kill(pid, 0)
            return True
    except (OSError, Exception):
        return False


def _kill_pid_safe(pid: int):
    try:
        if os.name == "nt":
            import subprocess as _sp
            _sp.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)
            import time as _time
            for _ in range(10):
                _time.sleep(0.5)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return
            os.kill(pid, 9)
    except Exception:
        pass

