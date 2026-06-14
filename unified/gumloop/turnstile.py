from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Any

from ..solver_manager import solver_manager

log = logging.getLogger("unified.gumloop.turnstile")

TURNSTILE_SITEKEY = "0x4AAAAAACMum7HpvvFmcf2r"
TURNSTILE_URL = "https://www.gumloop.com"
TURNSTILE_ACTION = "websocket_connect"
TOKEN_TTL = 250
MAX_SOLVE_ATTEMPTS = 3
SOLVE_RETRY_DELAY = 5
POOL_SIZE = int(os.getenv("GL_TURNSTILE_POOL_SIZE", "0"))
_MAX_SOLVE_HISTORY = 100


def _get_solver_proxy() -> str:
    proxy_file = Path(__file__).resolve().parent.parent / "data" / "solver_proxy.txt"
    try:
        if proxy_file.exists():
            return proxy_file.read_text(encoding="utf-8").strip()
    except OSError:
        pass
    return ""


class TurnstileSolver:

    def __init__(self, captcha_api_key: str = "", provider: str = "auto"):
        self._api_key: str = captcha_api_key
        self._provider: str = provider
        self._pool: list[tuple[str, float]] = []
        self._refill_tasks: list[asyncio.Task[None]] = []
        self._solve_lock: asyncio.Lock = asyncio.Lock()
        self.solve_count: int = 0
        self.solve_errors: int = 0
        self.solve_history: list[dict[str, Any]] = []
        self._sse_subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._solve_id_counter: int = 0
        self._ready_token: str | None = None
        self._ready_at: float = 0
        self._prefetch_task: asyncio.Task[None] | None = None

    def _next_solve_id(self) -> int:
        self._solve_id_counter += 1
        return self._solve_id_counter

    def subscribe_sse(self) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=50)
        self._sse_subscribers.append(q)
        return q

    def unsubscribe_sse(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._sse_subscribers.remove(q)
        except ValueError:
            pass

    def _broadcast(self, event: dict[str, Any]) -> None:
        dead: list[asyncio.Queue[dict[str, Any]]] = []
        for q in self._sse_subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            try:
                self._sse_subscribers.remove(q)
            except ValueError:
                pass

    def _emit_start(self, solve_id: int, provider: str, proxy: str) -> None:
        self._broadcast({
            "event": "solve_start",
            "id": solve_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider,
            "proxy": proxy,
        })

    def _emit_complete(self, solve_id: int, provider: str, status: str, solve_time_s: float, token_len: int, proxy: str, error: str) -> None:
        self._broadcast({
            "event": "solve_complete",
            "id": solve_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider,
            "status": status,
            "solve_time_s": round(solve_time_s, 2),
            "token_len": token_len,
            "proxy": proxy,
            "error": error,
        })

    def _record_solve(self, solve_id: int, provider: str, status: str, solve_time_s: float, token_len: int = 0, proxy: str = "", error: str = "") -> None:
        entry = {
            "id": solve_id,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "provider": provider,
            "status": status,
            "solve_time_s": round(solve_time_s, 2),
            "token_len": token_len,
            "proxy": proxy,
            "error": error,
        }
        self.solve_history.append(entry)
        if len(self.solve_history) > _MAX_SOLVE_HISTORY:
            self.solve_history[:] = self.solve_history[-_MAX_SOLVE_HISTORY:]
        self._emit_complete(solve_id, provider, status, solve_time_s, token_len, proxy, error)

    def update_api_key(self, key: str) -> None:
        self._api_key = key

    def update_provider(self, provider: str) -> None:
        self._provider = provider

    async def _solve_selfhost(self) -> str | None:
        proxy = _get_solver_proxy()
        sid = self._next_solve_id()
        self._emit_start(sid, "selfhost", proxy)
        start = time.time()
        try:
            token = await solver_manager.solve_token(
                sitekey=TURNSTILE_SITEKEY,
                url=TURNSTILE_URL,
                action=TURNSTILE_ACTION,
            )
            elapsed = time.time() - start
            if token:
                log.info("[turnstile] Self-host solver returned token (len=%d, %.1fs)", len(token), elapsed)
                self.solve_count += 1
                self._record_solve(sid, "selfhost", "success", elapsed, len(token), proxy)
                return token
            log.warning("[turnstile] Self-host solver returned empty token")
            self.solve_errors += 1
            self._record_solve(sid, "selfhost", "error", elapsed, 0, proxy, "empty token")
            return None
        except Exception as e:
            elapsed = time.time() - start
            log.error("[turnstile] Self-host solver failed: %s", e)
            self.solve_errors += 1
            self._record_solve(sid, "selfhost", "error", elapsed, 0, proxy, str(e))
            return None

    async def _solve(self) -> str | None:
        use_selfhost = self._provider in ("auto", "selfhost") and solver_manager.is_running()
        use_2captcha = self._provider in ("auto", "2captcha") and self._api_key

        if use_selfhost:
            token = await self._solve_selfhost()
            if token:
                return token
            if self._provider == "selfhost":
                return None
            log.warning("[turnstile] Self-host failed, falling back to 2Captcha")

        if not use_2captcha:
            return None
        sid = self._next_solve_id()
        self._emit_start(sid, "2captcha", "")
        start = time.time()
        try:
            from twocaptcha import TwoCaptcha

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
            token = ""
            if isinstance(result, dict):
                code = result.get("code", "")
                token = code if isinstance(code, str) else ""
            elapsed = time.time() - start
            if token:
                log.info("[turnstile] 2Captcha solved in %.1fs (len=%d)", elapsed, len(token))
                self.solve_count += 1
                self._record_solve(sid, "2captcha", "success", elapsed, len(token))
                return token
            log.warning("[turnstile] 2Captcha returned empty token")
            self.solve_errors += 1
            self._record_solve(sid, "2captcha", "error", elapsed, 0, "", "empty token")
            return None
        except Exception as e:
            elapsed = time.time() - start
            log.error("[turnstile] Solve failed: %s", e)
            self.solve_errors += 1
            self._record_solve(sid, "2captcha", "error", elapsed, 0, "", str(e))
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

    def _pop_fresh(self) -> str | None:
        now = time.time()
        while self._pool:
            token, ts = self._pool.pop(0)
            if (now - ts) < TOKEN_TTL:
                return token
        return None

    async def get_token(self) -> str | None:
        if not self._api_key and not solver_manager.is_running():
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
