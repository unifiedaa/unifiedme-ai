"""Unified AI Proxy — FastAPI application entry point with CLI license flow."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from .env_loader import load_local_env

load_local_env()

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse

from . import database as db
from .config import LISTEN_HOST, LISTEN_PORT, BASE_DIR, DATA_DIR, VERSION, CENTRAL_API_URL
from .router_proxy import router as proxy_router
from .router_admin import router as admin_router
from .router_vps import router as vps_router
from .router_terminal import router as terminal_router
from .router_explorer import router as explorer_router
from .router_chat import router as chat_router
from .proxy_kiro import close_all_clients as close_kiro
from .proxy_codebuddy import close_all_clients as close_codebuddy
from .proxy_wavespeed import close_all_clients as close_wavespeed
from .proxy_gumloop import close_all_clients as close_gumloop
from .chatbai.proxy import close_all_clients as close_chatbai
from .proxy_windsurf import close_all_clients as close_windsurf
from .proxy_therouter import close_all_clients as close_therouter
from . import license_client

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("unified")

# License file path (persisted after first input)
LICENSE_FILE = DATA_DIR / ".license"


class QuietAccessFilter(logging.Filter):
    """Suppress noisy polling endpoints from uvicorn access log (only 200 OK)."""
    _quiet_paths = ("/api/logs", "/api/batch/status", "/api/stats", "/favicon.ico")

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if "200" not in msg:
            return True
        for path in self._quiet_paths:
            if f"GET {path}" in msg:
                return False
        return True


logging.getLogger("uvicorn.access").addFilter(QuietAccessFilter())


# ---------------------------------------------------------------------------
# CLI: License input + validation (runs BEFORE uvicorn)
# ---------------------------------------------------------------------------

_LICENSE_PATTERN = re.compile(r'^UNIF-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}-[A-Z0-9]{5}$')


def _print_banner():
    print()
    print("  +======================================+")
    print(f"  |     Unified AI Proxy v{VERSION:<14s}|")
    print("  +======================================+")
    print()

    # Non-blocking update check
    try:
        import httpx
        logging.getLogger("httpx").setLevel(logging.WARNING)
        resp = httpx.get(f"{CENTRAL_API_URL}/api/version", timeout=3)
        data = resp.json()
        latest = data.get("version", "")
        if latest and latest != VERSION:
            print(f"  ** New version available: v{latest} **")
            print(f"  ** Run: unifiedme update **")
            print()
    except Exception:
        pass


def _load_saved_license() -> str:
    """Load license key from file or env var."""
    # Env var takes priority
    env_key = os.getenv("LICENSE_KEY", "").strip()
    if env_key:
        return env_key
    # Check saved file
    if LICENSE_FILE.exists():
        saved = LICENSE_FILE.read_text().strip()
        if saved:
            return saved
    return ""


def _save_license(key: str) -> None:
    """Save license key to local file."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    LICENSE_FILE.write_text(key)


def _prompt_license() -> str:
    """Interactive prompt for license key."""
    while True:
        try:
            key = input("  Enter license key: ").strip().upper()
        except (EOFError, KeyboardInterrupt):
            print("\n  Aborted.")
            sys.exit(1)

        if not key:
            print("  License key is required.\n")
            continue

        if not _LICENSE_PATTERN.match(key):
            print("  Invalid format. Expected: UNIF-XXXXX-XXXXX-XXXXX-XXXXX-XXXXX\n")
            continue

        return key


def _validate_license_sync(key: str) -> dict[str, object]:
    """Validate license against central API (synchronous wrapper)."""
    import httpx
    logging.getLogger("httpx").setLevel(logging.WARNING)

    fingerprint = license_client._generate_fingerprint()
    pc_name = platform.node() or "unknown"
    os_name = f"{platform.system()} {platform.release()}"
    machine_id = license_client._get_machine_id()

    try:
        resp = httpx.post(
            f"{license_client.CENTRAL_API_URL}/api/auth/activate",
            json={
                "license_key": key,
                "device_fingerprint": fingerprint,
                "device_name": pc_name,
                "os": os_name,
                "pc_name": pc_name,
                "machine_id": machine_id,
            },
            timeout=15,
        )
        return resp.json()
    except Exception as e:
        return {"error": f"Cannot reach license server: {e}"}


def _license_result_parts(result: dict[str, object]) -> tuple[bool, dict[str, object], str, bool, str]:
    ok = bool(result.get("ok"))
    license_obj = result.get("license")
    license_data = license_obj if isinstance(license_obj, dict) else {}
    device_id_obj = result.get("device_id")
    device_id = device_id_obj if isinstance(device_id_obj, str) and device_id_obj else "?"
    is_new = bool(result.get("is_new"))
    error_obj = result.get("error")
    error = error_obj if isinstance(error_obj, str) and error_obj else "Unknown error"
    return ok, license_data, device_id, is_new, error


def cli_license_flow() -> str:
    """Run the CLI license flow. Returns validated license key or exits."""
    _print_banner()

    key = _load_saved_license()

    if key:
        print(f"  License: {key}")
        print("  Validating...", end=" ", flush=True)
        result = _validate_license_sync(key)
        ok, lic, device_id, is_new, error = _license_result_parts(result)

        if ok:
            print("OK")
            print()
            print(f"  Owner:        {lic.get('owner_name', '?')}")
            print(f"  Tier:         {lic.get('tier', '?')}")
            print(f"  Max Devices:  {lic.get('max_devices', '?')}")
            print(f"  Max Accounts: {lic.get('max_accounts', '?')}")
            print(f"  Device ID:    {device_id}")
            if is_new:
                print("  Status:       NEW device bound")
            print()
            return key
        else:
            print("FAILED")
            print(f"  Error: {error}")
            print()
            # Saved key is invalid — clear it and prompt
            if LICENSE_FILE.exists():
                LICENSE_FILE.unlink()
    else:
        print("  No license key found.\n")

    # Interactive prompt
    while True:
        key = _prompt_license()
        print("  Validating...", end=" ", flush=True)
        result = _validate_license_sync(key)
        ok, lic, device_id, is_new, error = _license_result_parts(result)

        if ok:
            print("OK")
            print()
            print(f"  Owner:        {lic.get('owner_name', '?')}")
            print(f"  Tier:         {lic.get('tier', '?')}")
            print(f"  Max Devices:  {lic.get('max_devices', '?')}")
            print(f"  Max Accounts: {lic.get('max_accounts', '?')}")
            print(f"  Device ID:    {device_id}")
            if is_new:
                print("  Status:       NEW device bound")
            print()

            # Save for next time
            _save_license(key)
            print(f"  License saved to {LICENSE_FILE}")
            print()
            return key
        else:
            print("FAILED")
            print(f"  Error: {error}")
            print("  Try again.\n")


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    log.info("Initializing database...")
    await db.init_db()

    # Seed default filter rules
    seeded = await db.seed_default_filters()
    if seeded:
        log.info("Seeded %d default filter rules", seeded)

    # Generate default API key if none exists
    keys = await db.get_api_keys()
    if not keys:
        key_id, full_key = await db.create_api_key("default")
        log.info("Generated default API key: %s", full_key)
        log.info("Save this key — it will not be shown again in logs.")
    else:
        log.info("Found %d existing API key(s)", len(keys))

    # License already validated in CLI flow — just activate in async context
    await license_client.activate()
    license_client.start_sync_loop()

    # D1 = source of truth. Full pull on startup.
    import platform as _platform
    from datetime import datetime, timezone, timedelta
    _wib = timezone(timedelta(hours=7))
    _now_wib = datetime.now(_wib).strftime("%d %b %Y %H:%M:%S WIB")
    _device = _platform.node() or "unknown"
    _os_info = f"{_platform.system()} {_platform.release()}"

    try:
        # Pull ALL from D1 → replace local cache. D1 = pusat.
        pull_result = await license_client.full_pull_replace_local()
        if pull_result.get("error"):
            log.warning("D1 pull failed: %s — using local data", pull_result["error"])
        else:
            local_after = await db.get_accounts()
            _kr = sum(1 for a in local_after if a.get("kiro_status") == "ok")
            _cb = sum(1 for a in local_after if a.get("cb_status") == "ok")
            _ws = sum(1 for a in local_after if a.get("ws_status") == "ok")
            _gl = sum(1 for a in local_after if a.get("gl_status") == "ok")
            _cbai = sum(1 for a in local_after if a.get("cbai_status") == "ok")
            _skb = sum(1 for a in local_after if a.get("skboss_status") == "ok")
            _wf = sum(1 for a in local_after if a.get("windsurf_status") == "ok")
            log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            log.info("  D1 Synced (pulled %d accounts)", pull_result.get("total", 0))
            log.info("  KR: %d  CB: %d  WS: %d  GL: %d  CBAI: %d  SKB: %d  WF: %d", _kr, _cb, _ws, _gl, _cbai, _skb, _wf)
            log.info("  %s · %s (%s)", _now_wib, _device, _os_info)
            log.info("  Heartbeat: every %ds", license_client.SYNC_INTERVAL)
            log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    except Exception as e:
        log.warning("D1 startup sync failed: %s — using local data", e)

    # Check if admin password is set
    admin_pw = await db.get_setting("admin_password_set", "")
    if not admin_pw:
        log.info("First time setup — open http://localhost:%d/dashboard to set your admin password", LISTEN_PORT)

    # Start Windsurf sidecar (non-blocking, lazy — only if Node.js + sidecar code exist)
    try:
        from .windsurf_manager import windsurf_sidecar
        if windsurf_sidecar.is_available():
            log.info("Starting Windsurf sidecar...")
            sidecar_ok = await windsurf_sidecar.start()
            if sidecar_ok:
                synced = await windsurf_sidecar.sync_accounts_from_db()
                _wf = sum(1 for a in (await db.get_accounts()) if a.get("windsurf_status") == "ok")
                log.info("Windsurf sidecar ready (port %d, %d accounts synced, %d in DB)",
                         windsurf_sidecar._port, synced, _wf)
            else:
                log.warning("Windsurf sidecar failed to start — windsurf-* models unavailable")
        else:
            log.info("Windsurf sidecar not available (Node.js or unified/windsurf/ missing)")
    except Exception as e:
        log.warning("Windsurf sidecar startup error: %s — windsurf-* models unavailable", e)

    try:
        from .solver_manager import solver_manager
        if solver_manager.is_available():
            log.info("Starting captcha solver...")
            solver_ok = await solver_manager.start()
            if solver_ok:
                log.info("Captcha solver ready (port %d)", solver_manager.PORT)
            else:
                log.warning("Captcha solver failed to start — using 2captcha fallback")
        else:
            log.info("Captcha solver not available (camoufox not installed)")
    except Exception as e:
        log.warning("Captcha solver startup error: %s", e)

    # Start periodic GL exhaustion recovery (every 60s)
    _gl_recovery_task: asyncio.Task[None] | None = None

    async def _periodic_gl_recovery():
        """Background loop: auto-recover GL accounts whose cooldown has expired."""
        while True:
            try:
                await asyncio.sleep(60)
                recovered = await db.recover_gl_exhausted_accounts()
                if recovered:
                    log.info("Periodic GL recovery: %d account(s) restored to ok", recovered)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("Periodic GL recovery error: %s", e)

    _gl_recovery_task = asyncio.create_task(_periodic_gl_recovery())

    log.info("Unified AI Proxy ready on port %d", LISTEN_PORT)

    yield

    # Shutdown
    log.info("Shutting down...")

    # Cancel periodic GL recovery
    if _gl_recovery_task and not _gl_recovery_task.done():
        _gl_recovery_task.cancel()
        try:
            await _gl_recovery_task
        except asyncio.CancelledError:
            pass

    # Final push ALL accounts to D1 before stopping
    try:
        if license_client.is_licensed():
            local_accounts = await db.get_accounts()
            push_result = await license_client.push_sync(accounts=local_accounts)
            from datetime import datetime, timezone, timedelta
            _wib = timezone(timedelta(hours=7))
            _now_wib = datetime.now(_wib).strftime("%d %b %Y %H:%M:%S WIB")
            if push_result.get("error"):
                log.warning("D1 final push failed: %s", push_result["error"])
            else:
                _pushed = push_result.get("accounts_upserted", 0)
                log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
                log.info("  D1 Updated ✓")
                log.info("  Accounts pushed: %d", _pushed)
                log.info("  Last update: %s", _now_wib)
                log.info("  Device: %s", platform.node() or "unknown")
                log.info("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    except Exception as e:
        log.warning("D1 final push error: %s", e)

    try:
        await license_client.stop_sync_loop()
    except Exception:
        pass
    try:
        await close_kiro()
    except Exception:
        pass
    try:
        await close_codebuddy()
    except Exception:
        pass
    try:
        await close_wavespeed()
    except Exception:
        pass
    try:
        await close_gumloop()
    except Exception:
        pass
    try:
        await close_chatbai()
    except Exception:
        pass
    try:
        await close_windsurf()
    except Exception:
        pass
    try:
        await close_therouter()
    except Exception:
        pass
    try:
        from .windsurf_manager import windsurf_sidecar
        await windsurf_sidecar.stop()
    except Exception:
        pass
    try:
        from .solver_manager import solver_manager
        await solver_manager.stop()
    except Exception:
        pass
    # NOTE: Do NOT stop tunnels or MCP servers here.
    # They run as independent daemons and should survive proxy restarts.
    try:
        await db.close_db()
    except Exception:
        pass
    log.info("Shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Unified AI Proxy",
    description="Merged Kiro + CodeBuddy proxy with account management",
    version=VERSION,
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount routers
app.include_router(proxy_router)
app.include_router(admin_router)
app.include_router(vps_router)
app.include_router(terminal_router)
app.include_router(explorer_router)
app.include_router(chat_router)


# ---------------------------------------------------------------------------
# Dashboard + Explorer
# ---------------------------------------------------------------------------

DASHBOARD_PATH = BASE_DIR / "dashboard.html"
EXPLORER_PATH = BASE_DIR / "explorer.html"
CHAT_PATH = BASE_DIR / "chat.html"


@app.get("/dashboard", response_class=HTMLResponse)
async def serve_dashboard():
    """Serve the admin dashboard."""
    if DASHBOARD_PATH.exists():
        return FileResponse(DASHBOARD_PATH, media_type="text/html")
    return HTMLResponse(
        "<html><body><h1>Dashboard not found</h1>"
        "<p>Place dashboard.html in the unified/ directory.</p></body></html>",
        status_code=200,
    )


@app.get("/explorer", response_class=HTMLResponse)
async def serve_explorer():
    """Serve the file explorer UI."""
    if EXPLORER_PATH.exists():
        return FileResponse(EXPLORER_PATH, media_type="text/html")
    return HTMLResponse(
        "<html><body><h1>Explorer not found</h1></body></html>",
        status_code=200,
    )


@app.get("/chat", response_class=HTMLResponse)
async def serve_chat():
    """Serve the AI chat UI."""
    if CHAT_PATH.exists():
        return FileResponse(CHAT_PATH, media_type="text/html")
    return HTMLResponse(
        "<html><body><h1>Chat not found</h1></body></html>",
        status_code=200,
    )


@app.get("/")
async def root():
    """Health check / info endpoint."""
    return {
        "service": "Unified AI Proxy",
        "version": VERSION,
        "endpoints": {
            "proxy": "/v1/chat/completions, /v1/messages, /v1/models",
            "admin": "/api/accounts, /api/keys, /api/stats, /api/batch/*",
            "dashboard": "/dashboard",
            "explorer": "/explorer",
            "chat": "/chat",
            "docs": "/docs",
        },
    }


# ---------------------------------------------------------------------------
# Admin password setup endpoint
# ---------------------------------------------------------------------------

@app.post("/api/setup-password")
async def setup_password(request: Request):
    """First-time password setup. Only works if no password is set yet."""
    body = await request.json()
    new_password = str(body.get("password", "")).strip()
    if not new_password or len(new_password) < 4:
        return {"error": "Password must be at least 4 characters"}

    # Check if already set
    existing = await db.get_setting("admin_password_set", "")
    if existing:
        return {"error": "Password already set. Use dashboard to change it."}

    # Save password
    from .config import ADMIN_PASSWORD
    # Update the runtime config
    from . import config as cfg
    cfg.ADMIN_PASSWORD = new_password
    # Persist to DB
    await db.set_setting("admin_password", new_password)
    await db.set_setting("admin_password_set", "1")

    return {"ok": True, "message": "Password set successfully"}


@app.get("/api/setup-status")
async def setup_status():
    """Check if first-time setup is needed."""
    pw_set = await db.get_setting("admin_password_set", "")
    return {"password_set": bool(pw_set)}


@app.get("/api/uptime")
async def get_uptime():
    """Return server uptime + version."""
    from .cli import get_uptime_seconds
    uptime = get_uptime_seconds()
    h, m, s = uptime // 3600, (uptime % 3600) // 60, uptime % 60
    return {
        "version": VERSION,
        "uptime_seconds": uptime,
        "uptime_human": f"{h}h {m}m {s}s",
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import uvicorn

    # Step 1: CLI license flow (interactive, before uvicorn starts)
    license_key = cli_license_flow()

    # Set env var so license_client.activate() picks it up in lifespan
    os.environ["LICENSE_KEY"] = license_key

    print(f"  Starting proxy on port {LISTEN_PORT}...")
    print()
    print(f"  Dashboard:  http://localhost:{LISTEN_PORT}/dashboard")
    print(f"  API:        http://localhost:{LISTEN_PORT}/v1/chat/completions")
    print()
    print("  First time? Open the dashboard to set your admin password")
    print("  and get your API key.")
    print()

    uvicorn.run(
        "unified.main:app",
        host=LISTEN_HOST,
        port=LISTEN_PORT,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
