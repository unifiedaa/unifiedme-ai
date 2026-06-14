from __future__ import annotations

import asyncio
import logging
import importlib
import os
import platform
import random
import shutil
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Protocol, cast

log = logging.getLogger("unified.captcha_solver")

DEFAULT_PORT = int(os.getenv("CAPTCHA_SOLVER_PORT", "5000"))
DEFAULT_THREADS = max(1, int(os.getenv("CAPTCHA_SOLVER_THREADS", "1")))
DEFAULT_MAX_CACHE_AGE = int(os.getenv("CAPTCHA_SOLVER_MAX_CACHE_AGE", "3600"))
DEFAULT_PROXY_SUPPORT = os.getenv("CAPTCHA_SOLVER_PROXY", "1").strip().lower() not in {"0", "false", "no", "off"}
DEFAULT_DEBUG = os.getenv("CAPTCHA_SOLVER_DEBUG", "0").strip().lower() in {"1", "true", "yes", "on"}
CLEANUP_INTERVAL_SECONDS = 60


class AppLike(Protocol):
    def before_serving(self, func: object) -> object: ...
    def route(self, rule: str, methods: list[str]) -> "RouteDecorator": ...
    def run(self, *, host: str, port: int, debug: bool) -> None: ...


class RouteDecorator(Protocol):
    def __call__(self, func: Callable[..., Any]) -> object: ...


class BrowserLike(Protocol):
    async def new_context(self, proxy: dict[str, str] | None = None) -> "ContextLike": ...


class ContextLike(Protocol):
    async def new_page(self) -> "PageLike": ...
    async def close(self) -> None: ...


class LocatorLike(Protocol):
    async def click(self, timeout: int = ...) -> None: ...


class PageLike(Protocol):
    async def route(self, pattern: str, handler: object) -> None: ...
    async def goto(self, url: str, wait_until: str = ...) -> None: ...
    async def wait_for_selector(self, selector: str, state: str = ..., timeout: int = ...) -> None: ...
    async def eval_on_selector(self, selector: str, expression: str) -> None: ...
    async def input_value(self, selector: str, timeout: int = ...) -> str: ...
    def locator(self, selector: str) -> LocatorLike: ...
    async def unroute(self, pattern: str) -> None: ...


class RequestLike(Protocol):
    resource_type: str


class RouteLike(Protocol):
    async def fulfill(self, *, status: int, content_type: str, body: str) -> None: ...
    async def continue_(self) -> None: ...


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_proxies() -> list[str]:
    data_proxy = Path(__file__).resolve().parent / "data" / "solver_proxy.txt"
    if data_proxy.exists():
        try:
            lines = [line.strip() for line in data_proxy.read_text(encoding="utf-8").splitlines() if line.strip()]
            if lines:
                return lines
        except OSError:
            pass

    candidates = [
        Path.cwd() / "proxies.txt",
        _project_root() / "proxies.txt",
        Path(__file__).resolve().parent / "proxies.txt",
    ]
    for path in candidates:
        if path.exists():
            try:
                return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
            except OSError as exc:
                log.warning("Failed reading proxies from %s: %s", path, exc)
                return []
    return []


def _parse_proxy(proxy: str) -> dict[str, str] | None:
    parts = proxy.split(":")
    if len(parts) == 3:
        scheme, host, port = parts
        return {"server": f"{scheme}://{host}:{port}"}
    if len(parts) == 5:
        scheme, host, port, username, password = parts
        return {
            "server": f"{scheme}://{host}:{port}",
            "username": username,
            "password": password,
        }
    if proxy.startswith(("http://", "https://", "socks5://", "socks5h://")):
        return {"server": proxy}
    log.warning("Ignoring invalid proxy format: %s", proxy)
    return None


class TurnstileAPIServer:
    def __init__(self, thread: int, proxy_support: bool, max_cache_age: int, debug: bool) -> None:
        quart_module = importlib.import_module("quart")
        self.app: AppLike = cast(AppLike, quart_module.Quart(__name__))
        self.debug: bool = debug
        self.results: dict[str, dict[str, Any]] = {}
        self.thread_count: int = max(1, thread)
        self.proxy_support: bool = proxy_support
        self.max_cache_age: int = max_cache_age
        self.browser_pool: asyncio.Queue[tuple[int, BrowserLike]] = asyncio.Queue()
        self._setup_routes()

    def _cleanup_expired_tasks(self) -> int:
        now = time.time()
        expired = [
            task_id
            for task_id, data in self.results.items()
            if now - data.get("created_at", now) > self.max_cache_age
        ]
        for task_id in expired:
            _ = self.results.pop(task_id, None)
        if expired:
            log.info("Cleaned up %d expired captcha task(s)", len(expired))
        return len(expired)

    async def _periodic_cleanup(self) -> None:
        while True:
            await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
            try:
                self._cleanup_expired_tasks()
            except Exception as exc:
                log.warning("Captcha cleanup error: %s", exc)

    def _setup_routes(self) -> None:
        self.app.before_serving(self._startup)
        self.app.route("/turnstile", methods=["POST"])(self.process_turnstile)
        self.app.route("/result", methods=["GET"])(self.get_result)

    async def _startup(self) -> None:
        await self._initialize_browser()
        _ = self._cleanup_expired_tasks()
        _ = asyncio.create_task(self._periodic_cleanup())
        log.info(
            "Captcha solver ready with %d browser(s), proxy_support=%s",
            self.thread_count,
            self.proxy_support,
        )

    async def _initialize_browser(self) -> None:
        camoufox_cls = importlib.import_module("camoufox.async_api").AsyncCamoufox
        camoufox = camoufox_cls(headless=True)
        for index in range(self.thread_count):
            browser = await camoufox.start()
            await self.browser_pool.put((index + 1, browser))
        log.info("Initialized Camoufox browser pool (%d)", self.browser_pool.qsize())

    async def _new_context(self, browser: BrowserLike) -> ContextLike:
        if not self.proxy_support:
            return await browser.new_context()

        proxies = _load_proxies()
        if not proxies:
            return await browser.new_context()

        proxy = _parse_proxy(random.choice(proxies))
        if not proxy:
            return await browser.new_context()
        return await browser.new_context(proxy=proxy)

    async def _solve_turnstile(
        self,
        task_id: str,
        url: str,
        sitekey: str,
        action: str | None = None,
        cdata: str | None = None,
        cf_selector: str = ".cf-turnstile",
    ) -> None:
        index, browser = await self.browser_pool.get()
        context: ContextLike | None = None
        page: PageLike | None = None
        start_time = time.time()

        extra_attrs = ""
        if action:
            extra_attrs += f' data-action="{action}"'
        if cdata:
            extra_attrs += f' data-cdata="{cdata}"'
        injected_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset=\"utf-8\">
  <script src=\"https://challenges.cloudflare.com/turnstile/v0/api.js\" async defer></script>
</head>
<body>
  <div class=\"cf-turnstile\" data-sitekey=\"{sitekey}\"{extra_attrs} style=\"width:70px\"></div>
</body>
</html>"""
        intercepted: dict[str, bool] = {"done": False}

        try:
            context = await self._new_context(browser)
            page = await context.new_page()

            async def _intercept(route: RouteLike, req: RequestLike) -> None:
                if not intercepted["done"] and req.resource_type == "document":
                    intercepted["done"] = True
                    await route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=injected_html,
                    )
                    return
                await route.continue_()

            await page.route("**/*", _intercept)
            await page.goto(url, wait_until="commit")
            await page.wait_for_selector("[name=cf-turnstile-response]", state="attached", timeout=30000)
            await page.eval_on_selector(cf_selector, "el => el.style.width = '70px'")

            token = ""
            for _ in range(30):
                try:
                    token = await page.input_value("[name=cf-turnstile-response]", timeout=2000)
                    if token:
                        break
                    await page.locator(cf_selector or ".cf-turnstile").click(timeout=5000)
                    await asyncio.sleep(0.5)
                except Exception:
                    await asyncio.sleep(0.5)

            elapsed = round(time.time() - start_time, 3)
            created_at = self.results.get(task_id, {}).get("created_at", time.time())
            if token:
                self.results[task_id] = {
                    "status": "success",
                    "token": token,
                    "elapsed_time": elapsed,
                    "created_at": created_at,
                }
                log.info("Solved captcha in %.3fs using browser %d", elapsed, index)
            else:
                self.results[task_id] = {
                    "status": "failed",
                    "error": "CAPTCHA_FAIL",
                    "elapsed_time": elapsed,
                    "created_at": created_at,
                }
                log.warning("Captcha solve timed out in %.3fs using browser %d", elapsed, index)
        except Exception as exc:
            elapsed = round(time.time() - start_time, 3)
            created_at = self.results.get(task_id, {}).get("created_at", time.time())
            self.results[task_id] = {
                "status": "failed",
                "error": "CAPTCHA_FAIL",
                "elapsed_time": elapsed,
                "created_at": created_at,
            }
            log.warning("Captcha solve failed on browser %d: %s", index, exc)
        finally:
            if page is not None:
                try:
                    await page.unroute("**/*")
                except Exception:
                    pass
            if context is not None:
                try:
                    await context.close()
                except Exception:
                    pass
            await self.browser_pool.put((index, browser))

    async def process_turnstile(self):
        quart_module = importlib.import_module("quart")
        try:
            data = await quart_module.request.get_json()
        except Exception:
            return quart_module.jsonify({"status": "error", "error": "Invalid JSON body"}), 400

        if not isinstance(data, dict):
            return quart_module.jsonify({"status": "error", "error": "Request body must be a JSON object"}), 400

        url = data.get("url") if isinstance(data.get("url"), str) else ""
        sitekey = data.get("sitekey") if isinstance(data.get("sitekey"), str) else ""
        action = data.get("action") if isinstance(data.get("action"), str) else None
        cdata = data.get("cdata") if isinstance(data.get("cdata"), str) else None
        cf_selector_raw = data.get("cf_selector")
        cf_selector = cf_selector_raw if isinstance(cf_selector_raw, str) and cf_selector_raw else ".cf-turnstile"
        if not url or not sitekey:
            return quart_module.jsonify({"status": "error", "error": "Both 'url' and 'sitekey' are required"}), 400

        task_id = str(uuid.uuid4())
        self.results[task_id] = {"status": "pending", "created_at": time.time()}
        _ = asyncio.create_task(
            self._solve_turnstile(
                task_id=task_id,
                url=url,
                sitekey=sitekey,
                action=action,
                cdata=cdata,
                cf_selector=cf_selector,
            )
        )
        return quart_module.jsonify({"status": "created", "task_id": task_id}), 202

    async def get_result(self):
        quart_module = importlib.import_module("quart")
        task_id = quart_module.request.args.get("id", "")
        if not task_id or task_id not in self.results:
            return quart_module.jsonify({"status": "error", "error": "Invalid task ID"}), 404

        result = self.results[task_id]
        status = result.get("status", "pending")
        if status == "pending":
            return quart_module.jsonify({"status": "pending"}), 202
        if status == "success":
            return quart_module.jsonify(
                {
                    "status": "success",
                    "data": {
                        "token": result.get("token", ""),
                        "elapsed_time": result.get("elapsed_time"),
                    },
                }
            ), 200
        return quart_module.jsonify(
            {
                "status": "error",
                "error": result.get("error", "CAPTCHA_FAIL"),
                "elapsed_time": result.get("elapsed_time"),
            }
        ), 500


def create_app(
    thread: int = DEFAULT_THREADS,
    proxy_support: bool = DEFAULT_PROXY_SUPPORT,
    max_cache_age: int = DEFAULT_MAX_CACHE_AGE,
    debug: bool = DEFAULT_DEBUG,
) -> AppLike:
    server = TurnstileAPIServer(
        thread=thread,
        proxy_support=proxy_support,
        max_cache_age=max_cache_age,
        debug=debug,
    )
    return server.app


def _reexec_with_xvfb_if_needed() -> None:
    if platform.system() != "Linux":
        return
    if os.getenv("DISPLAY"):
        return
    if os.getenv("_CAPTCHA_SOLVER_XVFB") == "1":
        return
    xvfb_run = shutil.which("xvfb-run")
    if not xvfb_run:
        return
    env = {**os.environ, "_CAPTCHA_SOLVER_XVFB": "1"}
    os.execvpe(xvfb_run, [xvfb_run, "-a", sys.executable, *sys.argv], env)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.DEBUG if DEFAULT_DEBUG else logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    _reexec_with_xvfb_if_needed()
    app = create_app()
    app.run(host="127.0.0.1", port=DEFAULT_PORT, debug=DEFAULT_DEBUG)
