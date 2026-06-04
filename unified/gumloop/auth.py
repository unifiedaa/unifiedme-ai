"""Gumloop Firebase Auth — per-account token management.

Each account gets its own GumloopAuth instance. No singleton.
Google OAuth accounts only — no email/password login.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import httpx

log = logging.getLogger("unified.gumloop.auth")


class GumloopAuthError(Exception):
    """Base exception for Gumloop auth errors."""
    pass


class UserDisabledError(GumloopAuthError):
    """Raised when the Firebase user is disabled (USER_DISABLED).
    
    The Firebase Auth user record has been disabled (likely by Gumloop).
    Stored refresh tokens cannot be used. Account needs browser re-login
    to obtain fresh tokens, or the account itself must be replaced.
    """
    pass

FIREBASE_API_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
FIREBASE_REFRESH_URL = f"https://securetoken.googleapis.com/v1/token?key={FIREBASE_API_KEY}"


def _get_http_client(
    proxy_url: str | None = None, timeout: float = 30.0
) -> httpx.AsyncClient:
    if proxy_url:
        return httpx.AsyncClient(proxy=proxy_url, timeout=timeout)
    return httpx.AsyncClient(timeout=timeout)


class GumloopAuth:
    """Per-account Firebase auth with automatic token refresh."""

    def __init__(
        self,
        refresh_token: str,
        user_id: str,
        id_token: str = "",
        proxy_url: str | None = None,
    ):
        self.refresh_token = refresh_token
        self.user_id = user_id
        self.id_token = id_token
        self.proxy_url = proxy_url
        self.expires_at: float = 0  # Force refresh on first use
        self._lock = asyncio.Lock()

    async def refresh(self) -> dict[str, Any]:
        """Refresh Firebase token via refresh_token endpoint."""
        if not self.refresh_token:
            raise ValueError("No refresh_token available")

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token,
        }
        async with _get_http_client(self.proxy_url) as client:
            resp = await client.post(FIREBASE_REFRESH_URL, data=payload)
            if resp.status_code != 200:
                detail = resp.text[:500]
                # Detect USER_DISABLED — account needs browser re-login
                if '"USER_DISABLED"' in detail:
                    raise UserDisabledError(
                        f"Firebase token refresh failed (HTTP {resp.status_code}): {detail}"
                    )
                raise httpx.HTTPStatusError(
                    f"Firebase token refresh failed (HTTP {resp.status_code}): {detail}",
                    request=resp.request, response=resp,
                )
            data = resp.json()

        self.id_token = data.get("id_token", self.id_token)
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.user_id = data.get("user_id", self.user_id)
        expires_in = int(data.get("expires_in", 3600))
        self.expires_at = time.time() + expires_in - 300  # Refresh 5min early
        return data

    async def get_token(self) -> str:
        """Get a valid id_token, refreshing if expired. Thread-safe."""
        async with self._lock:
            if not self.id_token or time.time() >= self.expires_at:
                await self.refresh()
            return self.id_token

    def get_updated_tokens(self) -> dict[str, str]:
        """Return current tokens for DB persistence."""
        return {
            "gl_id_token": self.id_token or "",
            "gl_refresh_token": self.refresh_token or "",
            "gl_user_id": self.user_id or "",
        }
