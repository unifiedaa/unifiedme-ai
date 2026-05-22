"""Cloudflare Turnstile captcha solver via 2Captcha.

Strategy:
- Maintain a pool of pre-solved tokens (POOL_SIZE).
- Tokens are single-use, valid for ~280s if unused.
- On get_token(): pop from pool instantly, refill in background.
- Pool auto-refills to keep POOL_SIZE tokens ready at all times.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Optional

log = logging.getLogger("unified.gumloop.turnstile")

TURNSTILE_SITEKEY = "0x4AAAAAACMum7HpvvFmcf2r"
TURNSTILE_URL = "https://www.gumloop.com"
TURNSTILE_ACTION = "websocket_connect"
TOKEN_TTL = 250
MAX_SOLVE_ATTEMPTS = 3
SOLVE_RETRY_DELAY = 5
POOL_SIZE = max(5, int(os.getenv("GL_TURNSTILE_POOL_SIZE", "5")))


class TurnstileSolver:

    def __init__(self, captcha_api_key: str = ""):
        self._api_key = captcha_api_key
        self._pool: list[tuple[str, float]] = []
        self._refill_tasks: list[asyncio.Task] = []
        self._solve_lock = asyncio.Lock()
        self.solve_count: int = 0
        self.solve_errors: int = 0
        self._ready_token: Optional[str] = None
        self._ready_at: float = 0
        self._prefetch_task: Optional[asyncio.Task] = None

    def update_api_key(self, key: str) -> None:
        self._api_key = key

    async def _solve(self) -> Optional[str]:
        if not self._api_key:
            return None
        try:
            from twocaptcha import TwoCaptcha

            start = time.time()
            solver = TwoCaptcha(self._api_key, defaultTimeout=120, pollingInterval=5)
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None,
                lambda: solver.turnstile(
                    sitekey=TURNSTILE_SITEKEY,
                    url=TURNSTILE_URL,
                    action=TURNSTILE_ACTION,
                ),
            )
            token = result.get("code", "")
            elapsed = time.time() - start
            if token:
                log.info("[turnstile] Solved in %.1fs (len=%d)", elapsed, len(token))
                self.solve_count += 1
                return token
            log.warning("[turnstile] 2Captcha returned empty token")
            self.solve_errors += 1
            return None
        except Exception as e:
            log.error("[turnstile] Solve failed: %s", e)
            self.solve_errors += 1
            return None

    def _refill_pool(self) -> None:
        self._refill_tasks = [t for t in self._refill_tasks if not t.done()]
        fresh_count = sum(1 for _, ts in self._pool if (time.time() - ts) < TOKEN_TTL)
        needed = POOL_SIZE - fresh_count - len(self._refill_tasks)
        for _ in range(max(0, needed)):
            try:
                task = asyncio.create_task(self._solve_and_store())
                self._refill_tasks.append(task)
            except RuntimeError:
                break

    async def _solve_and_store(self) -> None:
        token = await self._solve()
        if token:
            self._pool.append((token, time.time()))
            log.info("[turnstile] Pool token ready (pool_size=%d)", len(self._pool))

    def _pop_fresh(self) -> Optional[str]:
        now = time.time()
        while self._pool:
            token, ts = self._pool.pop(0)
            if (now - ts) < TOKEN_TTL:
                return token
        return None

    async def get_token(self) -> Optional[str]:
        if not self._api_key:
            return None

        async with self._solve_lock:
            token = self._pop_fresh()
            if token:
                log.info("[turnstile] Using pool token (pool_remaining=%d)", len(self._pool))
                self._refill_pool()
                return token

            self._refill_tasks = [t for t in self._refill_tasks if not t.done()]
            if self._refill_tasks:
                log.info("[turnstile] Waiting for in-flight pool solve...")
                try:
                    await asyncio.wait_for(self._refill_tasks[0], timeout=130)
                except (asyncio.TimeoutError, Exception):
                    pass
                token = self._pop_fresh()
                if token:
                    self._refill_pool()
                    return token

            for attempt in range(1, MAX_SOLVE_ATTEMPTS + 1):
                log.info("[turnstile] Solving fresh token (attempt %d/%d)...", attempt, MAX_SOLVE_ATTEMPTS)
                token = await self._solve()
                if token:
                    self._refill_pool()
                    return token
                if attempt < MAX_SOLVE_ATTEMPTS:
                    log.warning("[turnstile] Solve failed on attempt %d, retrying in %ds...", attempt, SOLVE_RETRY_DELAY)
                    await asyncio.sleep(SOLVE_RETRY_DELAY)

            log.error("[turnstile] All %d solve attempts failed", MAX_SOLVE_ATTEMPTS)
            return None

    def start_pool(self) -> None:
        self._refill_pool()

    def close(self) -> None:
        for t in self._refill_tasks:
            if not t.done():
                t.cancel()
        self._refill_tasks = []
        self._pool = []
