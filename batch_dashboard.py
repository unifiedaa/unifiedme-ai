#!/usr/bin/env python3
"""
Batch Account Login Dashboard
Single-file web app for automating Kiro + CodeBuddy account registration.
Runs on port 1432, serves UI + handles login via subprocess + SSE streaming.
"""

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import aiohttp
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
LISTEN_HOST = os.getenv("DASH_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("DASH_PORT", "1432"))
DASH_PASSWORD = os.getenv("DASH_PASSWORD", "kUcingku0")

AUTH_SCRIPT = Path(__file__).parent / "login.py"
PYTHON_BIN = Path(__file__).parent / ".venv" / "bin" / "python"
COOKIES_DIR = Path(__file__).parent.parent / "cookies"

KIRO_ADMIN_URL = os.getenv("KIRO_ADMIN_URL", "http://127.0.0.1:1430")
KIRO_ADMIN_PASSWORD = os.getenv("KIRO_ADMIN_PASSWORD", "kUcingku0")

CODEBUDDY_CREDS_DIR = Path(
    os.getenv("CODEBUDDY_CREDS_DIR", "/root/codebuddy-proxy/.codebuddy_creds")
)
GUMLOOP_SCRIPT = Path(__file__).parent / "gumloop_login.py"

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class AccountJob:
    id: str
    email: str
    password: str
    providers: list[str]
    status: JobStatus = JobStatus.QUEUED
    logs: list[dict] = field(default_factory=list)
    result: Optional[dict] = None
    started_at: float = 0
    finished_at: float = 0


class BatchState:
    def __init__(self):
        self.jobs: list[AccountJob] = []
        self.running = False
        self.cancelled = False
        self.gumloop_mode: str = "none"  # "none", "relogin", "full"
        self._sse_queues: list[asyncio.Queue] = []
        self._lock = asyncio.Lock()

    def broadcast(self, event: dict):
        dead = []
        for q in self._sse_queues:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_queues.remove(q)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._sse_queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._sse_queues:
            self._sse_queues.remove(q)


state = BatchState()

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="Batch Login Dashboard")


class BatchRequest(BaseModel):
    accounts: list[str]
    providers: list[str]
    gumloop_mode: str = "none"


def _auth_ok(request: Request) -> bool:
    if not DASH_PASSWORD:
        return True
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return auth[7:] == DASH_PASSWORD
    cookie_pw = request.cookies.get("dash_password", "")
    return cookie_pw == DASH_PASSWORD


# ---------------------------------------------------------------------------
# API endpoints
# ---------------------------------------------------------------------------

@app.post("/api/start")
async def start_batch(req: BatchRequest, request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, 401)

    if state.running:
        return JSONResponse({"error": "batch already running"}, 409)

    # Parse accounts
    accounts = []
    for line in req.accounts:
        line = line.strip()
        if not line or ":" not in line:
            continue
        parts = line.split(":", 1)
        accounts.append((parts[0].strip(), parts[1].strip()))

    if not accounts:
        return JSONResponse({"error": "no valid accounts"}, 400)

    providers = [p for p in req.providers if p in ("kiro", "codebuddy", "gumloop")]
    if not providers:
        return JSONResponse({"error": "no providers selected"}, 400)

    state.jobs.clear()
    state.cancelled = False
    state.gumloop_mode = req.gumloop_mode if req.gumloop_mode in ("relogin", "full") else "none"
    for email, password in accounts:
        job = AccountJob(
            id=str(uuid.uuid4())[:8],
            email=email,
            password=password,
            providers=providers,
        )
        state.jobs.append(job)

    # Start processing in background
    asyncio.create_task(_run_batch())

    return {"ok": True, "count": len(state.jobs)}


@app.post("/api/cancel")
async def cancel_batch(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    state.cancelled = True
    return {"ok": True}


@app.get("/api/status")
async def get_status(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    return {
        "running": state.running,
        "jobs": [
            {
                "id": j.id,
                "email": j.email,
                "providers": j.providers,
                "status": j.status.value,
                "logs_count": len(j.logs),
            }
            for j in state.jobs
        ],
    }


@app.get("/api/events")
async def sse_events(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, 401)

    queue = state.subscribe()

    async def event_stream():
        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=30)
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                except asyncio.CancelledError:
                    break
        finally:
            state.unsubscribe(queue)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/", response_class=HTMLResponse)
async def serve_ui():
    return DASHBOARD_HTML


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

async def _run_batch():
    state.running = True
    state.broadcast({"type": "batch_start", "total": len(state.jobs)})

    for i, job in enumerate(state.jobs):
        if state.cancelled:
            job.status = JobStatus.CANCELLED
            state.broadcast({
                "type": "job_cancelled",
                "job_id": job.id,
                "email": job.email,
                "index": i,
            })
            continue

        job.status = JobStatus.RUNNING
        job.started_at = time.time()
        state.broadcast({
            "type": "job_start",
            "job_id": job.id,
            "email": job.email,
            "providers": job.providers,
            "index": i,
            "total": len(state.jobs),
        })

        try:
            result = await _run_login(job)

            if "gumloop" in job.providers:
                if state.gumloop_mode == "relogin":
                    gl_result = await _run_gumloop_relogin(job)
                else:
                    gl_result = await _run_gumloop_full(job)
                result.update(gl_result)

            job.result = result

            imported = []
            if "kiro" in job.providers and result.get("kiro", {}).get("success"):
                ok = await _import_kiro(job.email, result["kiro"])
                if ok:
                    imported.append("kiro")
            if "codebuddy" in job.providers and result.get("codebuddy", {}).get("success"):
                ok = await _import_codebuddy(job.email, result["codebuddy"])
                if ok:
                    imported.append("codebuddy")
            if "gumloop" in job.providers and result.get("gumloop", {}).get("success"):
                ok = await _import_gumloop(job.email, result["gumloop"])
                if ok:
                    imported.append("gumloop")

            any_success = bool(imported)
            job.status = JobStatus.SUCCESS if any_success else JobStatus.FAILED
            job.finished_at = time.time()

            state.broadcast({
                "type": "job_done",
                "job_id": job.id,
                "email": job.email,
                "status": job.status.value,
                "imported": imported,
                "duration": round(job.finished_at - job.started_at, 1),
                "kiro": result.get("kiro", {}).get("success", False),
                "codebuddy": result.get("codebuddy", {}).get("success", False),
                "gumloop": result.get("gumloop", {}).get("success", False),
            })

        except Exception as exc:
            job.status = JobStatus.FAILED
            job.finished_at = time.time()
            state.broadcast({
                "type": "job_error",
                "job_id": job.id,
                "email": job.email,
                "error": str(exc),
            })

    state.running = False
    summary = {
        "success": sum(1 for j in state.jobs if j.status == JobStatus.SUCCESS),
        "failed": sum(1 for j in state.jobs if j.status == JobStatus.FAILED),
        "cancelled": sum(1 for j in state.jobs if j.status == JobStatus.CANCELLED),
    }
    state.broadcast({"type": "batch_done", **summary})


async def _run_login(job: AccountJob) -> dict:
    """Run login.py as subprocess, stream output via SSE."""
    env = {
        **os.environ,
        "BATCHER_ENABLE_CAMOUFOX": "true",
        "BATCHER_CAMOUFOX_HEADLESS": "true",
        "BATCHER_KIRO_AUTH_DEBUG": "true",
        "BATCHER_CODEBUDDY_AUTH_DEBUG": "true",
    }

    if set(job.providers) == {"kiro"}:
        env["BATCHER_CONCURRENT"] = "1"
        env["BATCHER_PRIORITY"] = "standard"
    elif set(job.providers) == {"codebuddy"}:
        env["BATCHER_CONCURRENT"] = "1"
        env["BATCHER_PRIORITY"] = "max"
    else:
        env["BATCHER_CONCURRENT"] = "2"
        env["BATCHER_PRIORITY"] = "standard"

    proc = await asyncio.create_subprocess_exec(
        str(PYTHON_BIN),
        str(AUTH_SCRIPT),
        "--email", job.email,
        "--password", job.password,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    result_data = {}

    async def read_stream(stream, is_stderr=False):
        nonlocal result_data
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue

            log_entry = {"ts": time.time(), "raw": text}

            try:
                parsed = json.loads(text)
                log_entry["parsed"] = parsed

                if parsed.get("type") == "result":
                    result_data = parsed

                state.broadcast({
                    "type": "job_log",
                    "job_id": job.id,
                    "email": job.email,
                    "log_type": parsed.get("type", "info"),
                    "provider": parsed.get("provider", ""),
                    "step": parsed.get("step", ""),
                    "message": parsed.get("message", parsed.get("error", text)),
                })
            except json.JSONDecodeError:
                # Capture ALL output — stderr debug, warnings, errors
                log_type = "stderr" if is_stderr else "stdout"
                state.broadcast({
                    "type": "job_log",
                    "job_id": job.id,
                    "email": job.email,
                    "log_type": log_type,
                    "message": text,
                })

            job.logs.append(log_entry)

    await asyncio.gather(
        read_stream(proc.stdout),
        read_stream(proc.stderr, is_stderr=True),
    )
    await proc.wait()

    return result_data


async def _import_kiro(email: str, kiro_result: dict) -> bool:
    """Import Kiro tokens into Kiro-Go proxy via admin API."""
    creds = kiro_result.get("credentials", {})
    if not creds.get("access_token"):
        return False

    payload = {
        "accessToken": creds["access_token"],
        "refreshToken": creds.get("refresh_token", ""),
        "authMethod": "social",
        "provider": "Google",
        "region": "us-east-1",
        "email": email,
        "enabled": True,
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{KIRO_ADMIN_URL}/admin/api/auth/credentials",
                json=payload,
                headers={"X-Admin-Password": KIRO_ADMIN_PASSWORD},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                ok = resp.status == 200
                if ok:
                    state.broadcast({
                        "type": "import_ok",
                        "provider": "kiro",
                        "email": email,
                    })
                return ok
    except Exception as exc:
        state.broadcast({
            "type": "import_error",
            "provider": "kiro",
            "email": email,
            "error": str(exc),
        })
        return False


async def _import_codebuddy(email: str, cb_result: dict) -> bool:
    """Import CodeBuddy API key into codebuddy2api credentials dir."""
    creds = cb_result.get("credentials", {})
    api_key = creds.get("api_key", "")
    if not api_key:
        return False

    try:
        CODEBUDDY_CREDS_DIR.mkdir(parents=True, exist_ok=True)
        email_hash = hashlib.sha256(email.encode()).hexdigest()[:16]
        cred_file = CODEBUDDY_CREDS_DIR / f"{email_hash}.json"
        cred_data = {
            "bearer_token": api_key,
            "email": email,
            "created_at": int(time.time()),
            "expires_in": 86400,
        }
        cred_file.write_text(json.dumps(cred_data, indent=2))

        # Reload codebuddy-proxy credentials via API
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "http://127.0.0.1:1431/codebuddy/v1/credentials",
                    headers={"Authorization": f"Bearer {DASH_PASSWORD}"},
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    pass  # just trigger a read
        except Exception:
            pass

        state.broadcast({
            "type": "import_ok",
            "provider": "codebuddy",
            "email": email,
        })
        return True
    except Exception as exc:
        state.broadcast({
            "type": "import_error",
            "provider": "codebuddy",
            "email": email,
            "error": str(exc),
        })
        return False


# ---------------------------------------------------------------------------
# Gumloop login
# ---------------------------------------------------------------------------

async def _run_gumloop_relogin(job: AccountJob) -> dict:
    import httpx
    from unified import database as udb

    FIREBASE_KEY = "AIzaSyCYuXqbJ0YBNltoGS4-7Y6Hozrra8KKmaE"
    FIREBASE_AUTH_URL = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={FIREBASE_KEY}"

    state.broadcast({"type": "job_log", "job_id": job.id, "email": job.email,
        "log_type": "info", "provider": "gumloop", "step": "login",
        "message": "Firebase signInWithPassword..."})

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(FIREBASE_AUTH_URL, json={
                "email": job.email, "password": job.password,
                "returnSecureToken": True, "clientType": "CLIENT_TYPE_WEB",
            })
            data = resp.json()

        if resp.status_code != 200:
            err_msg = data.get("error", {}).get("message", f"HTTP {resp.status_code}")
            state.broadcast({"type": "job_log", "job_id": job.id, "email": job.email,
                "log_type": "error", "provider": "gumloop", "step": "login",
                "message": f"Firebase login failed: {err_msg}"})
            all_accts = await udb.get_accounts()
            acct = next((a for a in all_accts if a.get("email") == job.email), None)
            if acct and "USER_DISABLED" in err_msg.upper():
                await udb.update_account(acct["id"], gl_status="user_disabled", gl_error=err_msg)
            return {"gumloop": {"success": False, "error": err_msg}}

        id_token = data.get("idToken", "")
        refresh_token = data.get("refreshToken", "")
        user_id = data.get("localId", "")

        all_accts = await udb.get_accounts()
        acct = next((a for a in all_accts if a.get("email") == job.email), None)
        if acct:
            await udb.update_account(acct["id"],
                gl_id_token=id_token, gl_refresh_token=refresh_token,
                gl_user_id=user_id, gl_status="ok", gl_error="")

        state.broadcast({"type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "info", "provider": "gumloop", "step": "login",
            "message": "Firebase login OK"})

        return {"gumloop": {"success": True, "credentials": {
            "id_token": id_token, "refresh_token": refresh_token,
            "user_id": user_id,
            "gummie_id": acct.get("gl_gummie_id", "") if acct else "",
        }}}
    except Exception as e:
        state.broadcast({"type": "job_log", "job_id": job.id, "email": job.email,
            "log_type": "error", "provider": "gumloop", "step": "login",
            "message": f"Firebase login failed: {e}"})
        return {"gumloop": {"success": False, "error": str(e)}}


async def _run_gumloop_full(job: AccountJob) -> dict:
    env = {
        **os.environ,
        "BATCHER_CAMOUFOX_HEADLESS": "true",
    }

    proc = await asyncio.create_subprocess_exec(
        str(PYTHON_BIN), str(GUMLOOP_SCRIPT),
        "--email", job.email, "--password", job.password,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )

    result_data = {}

    async def read_stream(stream, is_stderr=False):
        nonlocal result_data
        while True:
            line = await stream.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
                if parsed.get("type") == "result":
                    result_data = parsed
                state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": parsed.get("type", "info"), "provider": "gumloop",
                    "step": parsed.get("step", ""),
                    "message": parsed.get("message", parsed.get("error", text)),
                })
            except json.JSONDecodeError:
                log_type = "stderr" if is_stderr else "stdout"
                state.broadcast({
                    "type": "job_log", "job_id": job.id, "email": job.email,
                    "log_type": log_type, "provider": "gumloop",
                    "step": "", "message": text,
                })

    await asyncio.gather(
        read_stream(proc.stdout),
        read_stream(proc.stderr, is_stderr=True),
    )
    await proc.wait()

    gl = result_data.get("gumloop", result_data)
    if isinstance(gl, dict) and "success" in gl:
        return {"gumloop": gl}
    return {"gumloop": {"success": False, "error": "No result from gumloop_login.py"}}


async def _import_gumloop(email: str, gl_result: dict) -> bool:
    from unified import database as udb

    creds = gl_result.get("credentials", {})
    if not creds.get("id_token"):
        return False

    try:
        all_accts = await udb.get_accounts()
        acct = next((a for a in all_accts if a.get("email") == email), None)
        if not acct:
            state.broadcast({"type": "import_error", "provider": "gumloop",
                "email": email, "error": "Account not found in DB"})
            return False

        await udb.update_account(acct["id"],
            gl_id_token=creds["id_token"],
            gl_refresh_token=creds.get("refresh_token", ""),
            gl_user_id=creds.get("user_id", ""),
            gl_gummie_id=creds.get("gummie_id", ""),
            gl_status="ok", gl_error="")

        state.broadcast({"type": "import_ok", "provider": "gumloop", "email": email})
        return True
    except Exception as exc:
        state.broadcast({"type": "import_error", "provider": "gumloop",
            "email": email, "error": str(exc)})
        return False


# ---------------------------------------------------------------------------
# Disabled GL accounts API
# ---------------------------------------------------------------------------

@app.get("/api/gl/disabled")
async def get_gl_disabled(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    from unified import database as udb
    all_accts = await udb.get_accounts()
    disabled = [a for a in all_accts if a.get("gl_status") == "user_disabled"]
    return {"accounts": [{"id": a["id"], "email": a.get("email", "")} for a in disabled]}


@app.post("/api/gl/delete")
async def delete_gl_disabled(request: Request):
    if not _auth_ok(request):
        return JSONResponse({"error": "unauthorized"}, 401)
    from unified import database as udb
    all_accts = await udb.get_accounts()
    disabled = [a for a in all_accts if a.get("gl_status") == "user_disabled"]
    deleted = 0
    for a in disabled:
        await udb.delete_account(a["id"])
        deleted += 1
    return {"message": f"Deleted {deleted} disabled accounts", "deleted": deleted}


# ---------------------------------------------------------------------------
# Frontend HTML
# ---------------------------------------------------------------------------

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Batch Login Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{
  --bg:#0f1117;--surface:#1a1d27;--surface2:#242836;--border:#2e3348;
  --text:#e4e6f0;--muted:#8b8fa3;--accent:#6c8aff;--accent2:#4f6fff;
  --green:#34d399;--red:#f87171;--yellow:#fbbf24;--orange:#fb923c;
}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);min-height:100vh}
.container{max-width:960px;margin:0 auto;padding:24px 16px}
h1{font-size:1.5rem;font-weight:700;margin-bottom:4px}
.subtitle{color:var(--muted);font-size:.85rem;margin-bottom:24px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}
.card-title{font-size:.9rem;font-weight:600;margin-bottom:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
textarea{width:100%;min-height:140px;background:var(--surface2);border:1px solid var(--border);border-radius:8px;padding:12px;color:var(--text);font-family:'JetBrains Mono',monospace;font-size:.85rem;resize:vertical;outline:none;transition:border-color .2s}
textarea:focus{border-color:var(--accent)}
textarea::placeholder{color:var(--muted)}
.controls{display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-top:12px}
.checkbox-group{display:flex;gap:16px}
.checkbox-label{display:flex;align-items:center;gap:6px;cursor:pointer;font-size:.9rem;user-select:none}
.checkbox-label input{width:16px;height:16px;accent-color:var(--accent)}
.btn{padding:10px 24px;border:none;border-radius:8px;font-size:.9rem;font-weight:600;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:6px}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent2)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed}
.btn-danger{background:var(--red);color:#fff}
.btn-danger:hover{background:#ef4444}
.btn-danger:disabled{opacity:.5;cursor:not-allowed}
.spacer{flex:1}
.stats{display:flex;gap:12px;font-size:.85rem}
.stat{padding:4px 10px;border-radius:6px;font-weight:600}
.stat-success{background:rgba(52,211,153,.15);color:var(--green)}
.stat-fail{background:rgba(248,113,113,.15);color:var(--red)}
.stat-pending{background:rgba(139,143,163,.15);color:var(--muted)}
.stat-running{background:rgba(108,138,255,.15);color:var(--accent)}

/* Log viewer */
.log-viewer{background:var(--bg);border:1px solid var(--border);border-radius:8px;min-height:300px;max-height:520px;overflow-y:auto;font-family:'JetBrains Mono',monospace;font-size:.8rem;padding:8px 12px;scroll-behavior:smooth}
.log-line{padding:2px 0;line-height:1.5;word-break:break-all}
.log-line .ts{color:var(--muted);margin-right:8px}
.log-line .provider{font-weight:600;margin-right:6px}
.log-line .provider.kiro{color:var(--accent)}
.log-line .provider.codebuddy{color:var(--orange)}
.log-line .provider.gumloop{color:var(--green)}
.log-line .provider.all{color:var(--yellow)}
.log-line.success{color:var(--green)}
.log-line.error{color:var(--red)}
.log-line.import{color:var(--green);font-weight:600}
.log-line.job-start{color:var(--accent);font-weight:600;border-top:1px solid var(--border);margin-top:6px;padding-top:6px}
.log-line.batch-done{color:var(--yellow);font-weight:700;border-top:1px solid var(--border);margin-top:8px;padding-top:8px}
.log-line.debug{color:var(--muted);opacity:.7}
.log-line.stderr{color:#a78bfa;opacity:.85}
.log-line.stdout{color:var(--muted);opacity:.8}

/* Job list */
.job-list{display:flex;flex-direction:column;gap:4px;margin-top:8px}
.job-item{display:flex;align-items:center;gap:8px;padding:6px 10px;border-radius:6px;font-size:.85rem;background:var(--surface2)}
.job-item .email{flex:1;font-family:'JetBrains Mono',monospace;font-size:.8rem}
.job-item .badge{padding:2px 8px;border-radius:4px;font-size:.75rem;font-weight:600;text-transform:uppercase}
.badge-queued{background:rgba(139,143,163,.2);color:var(--muted)}
.badge-running{background:rgba(108,138,255,.2);color:var(--accent)}
.badge-success{background:rgba(52,211,153,.2);color:var(--green)}
.badge-failed{background:rgba(248,113,113,.2);color:var(--red)}
.badge-cancelled{background:rgba(139,143,163,.2);color:var(--muted)}
.providers-tags{display:flex;gap:4px}
.providers-tags .tag{font-size:.7rem;padding:1px 6px;border-radius:3px;font-weight:600}
.tag-kiro{background:rgba(108,138,255,.2);color:var(--accent)}
.tag-codebuddy{background:rgba(251,146,36,.2);color:var(--orange)}
.tag-gumloop{background:rgba(52,211,153,.2);color:var(--green)}

.disabled-panel{margin-top:4px}
.disabled-list{display:flex;flex-direction:column;gap:3px;margin:8px 0;max-height:200px;overflow-y:auto}
.disabled-item{display:flex;align-items:center;justify-content:space-between;padding:4px 10px;border-radius:4px;background:var(--surface2);font-family:'JetBrains Mono',monospace;font-size:.8rem}
.disabled-item .email{color:var(--text)}.disabled-item .status{color:var(--red);font-size:.7rem;font-weight:600}
.disabled-actions{display:flex;gap:6px;margin-top:8px}
.disabled-count{font-size:.8rem;color:var(--muted);margin-left:8px}
@media(max-width:640px){.controls{flex-direction:column;align-items:stretch}.spacer{display:none}}
</style>
</head>
<body>
<div class="container">
  <h1>Batch Login Dashboard</h1>
  <p class="subtitle">Automate Kiro + CodeBuddy account registration</p>

  <div class="card">
    <div class="card-title">Accounts</div>
    <textarea id="accounts" placeholder="email1@gmail.com:password1&#10;email2@gmail.com:password2&#10;email3@gmail.com:password3"></textarea>
    <div class="controls">
      <div class="checkbox-group">
        <label class="checkbox-label"><input type="checkbox" id="cb-kiro" checked> Kiro</label>
        <label class="checkbox-label"><input type="checkbox" id="cb-codebuddy" checked> CodeBuddy</label>
        <label class="checkbox-label"><input type="checkbox" id="cb-gumloop" checked> Gumloop</label>
        <select id="gl-mode" style="font-size:.8rem;padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:var(--surface2);color:var(--text)">
          <option value="full">Full Login</option>
          <option value="relogin">Relogin Only</option>
        </select>
      </div>
      <div class="spacer"></div>
      <button class="btn btn-danger" id="btn-cancel" disabled onclick="cancelBatch()">Cancel</button>
      <button class="btn btn-primary" id="btn-start" onclick="startBatch()">&#9654; Start Batch</button>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:10px">
      <div class="card-title" style="margin-bottom:0">Progress</div>
      <div class="stats">
        <span class="stat stat-success" id="stat-success">0 ok</span>
        <span class="stat stat-fail" id="stat-fail">0 fail</span>
        <span class="stat stat-running" id="stat-running">0 running</span>
        <span class="stat stat-pending" id="stat-pending">0 queued</span>
      </div>
    </div>
    <div class="job-list" id="job-list"></div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <div class="card-title" style="margin-bottom:0">Logs</div>
      <button class="btn" style="padding:4px 12px;font-size:.75rem;background:var(--surface2);color:var(--muted)" onclick="clearLogs()">Clear</button>
    </div>
    <div class="log-viewer" id="log-viewer"></div>
  </div>

  <div class="card">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <div style="display:flex;align-items:center">
        <div class="card-title" style="margin-bottom:0">Disabled GL Accounts</div>
        <span class="disabled-count" id="disabled-count"></span>
      </div>
      <button class="btn" style="padding:4px 12px;font-size:.75rem;background:var(--surface2);color:var(--muted)" onclick="loadDisabled()">&#8635; Refresh</button>
    </div>
    <div class="disabled-panel">
      <div class="disabled-list" id="disabled-list"><span style="color:var(--muted);font-size:.8rem">Click Refresh to load</span></div>
      <div class="disabled-actions">
        <button class="btn" style="padding:4px 12px;font-size:.8rem;background:var(--surface2);color:var(--accent)" onclick="copyDisabledEmails()">&#128203; Copy All</button>
        <button class="btn btn-danger" style="padding:4px 12px;font-size:.8rem" onclick="deleteDisabledAccounts()">&#128465; Delete All</button>
      </div>
    </div>
  </div>
</div>

<script>
const PASSWORD = '';
let evtSource = null;
let autoScroll = true;

function getAuth() {
  let pw = localStorage.getItem('dash_pw') || '';
  if (!pw) {
    pw = prompt('Dashboard password:') || '';
    localStorage.setItem('dash_pw', pw);
  }
  return pw;
}

function headers() {
  return { 'Authorization': 'Bearer ' + getAuth(), 'Content-Type': 'application/json' };
}

function log(html, cls='') {
  const el = document.getElementById('log-viewer');
  const div = document.createElement('div');
  div.className = 'log-line ' + cls;
  div.innerHTML = html;
  el.appendChild(div);
  if (autoScroll) el.scrollTop = el.scrollHeight;
}

function clearLogs() {
  document.getElementById('log-viewer').innerHTML = '';
}

function ts() {
  return new Date().toLocaleTimeString('en-GB', {hour12:false});
}

function updateStats(jobs) {
  let s=0,f=0,r=0,q=0;
  (jobs||[]).forEach(j => {
    if(j.status==='success') s++;
    else if(j.status==='failed') f++;
    else if(j.status==='running') r++;
    else if(j.status==='queued') q++;
  });
  document.getElementById('stat-success').textContent = s + ' ok';
  document.getElementById('stat-fail').textContent = f + ' fail';
  document.getElementById('stat-running').textContent = r + ' running';
  document.getElementById('stat-pending').textContent = q + ' queued';
}

function renderJobs(jobs) {
  const el = document.getElementById('job-list');
  el.innerHTML = '';
  (jobs||[]).forEach(j => {
    const div = document.createElement('div');
    div.className = 'job-item';
    div.id = 'job-' + j.id;
    const provTags = (j.providers||[]).map(p =>
      `<span class="tag tag-${p}">${p}</span>`
    ).join('');
    div.innerHTML = `
      <span class="email">${j.email}</span>
      <div class="providers-tags">${provTags}</div>
      <span class="badge badge-${j.status}" id="badge-${j.id}">${j.status}</span>
    `;
    el.appendChild(div);
  });
}

function updateJobBadge(jobId, status) {
  const badge = document.getElementById('badge-' + jobId);
  if (badge) {
    badge.textContent = status;
    badge.className = 'badge badge-' + status;
  }
}

async function startBatch() {
  const text = document.getElementById('accounts').value.trim();
  if (!text) return alert('Enter at least one email:password');

  const providers = [];
  if (document.getElementById('cb-kiro').checked) providers.push('kiro');
  if (document.getElementById('cb-codebuddy').checked) providers.push('codebuddy');
  if (document.getElementById('cb-gumloop').checked) providers.push('gumloop');
  if (!providers.length) return alert('Select at least one provider');

  const accounts = text.split('\\n').map(l => l.trim()).filter(l => l && l.includes(':'));
  if (!accounts.length) return alert('No valid email:password lines found');

  document.getElementById('btn-start').disabled = true;
  document.getElementById('btn-cancel').disabled = false;
  clearLogs();

  try {
    const resp = await fetch('/api/start', {
      method: 'POST',
      headers: headers(),
      body: JSON.stringify({ accounts, providers, gumloop_mode: providers.includes('gumloop') ? document.getElementById('gl-mode').value : 'none' }),
    });
    const data = await resp.json();
    if (data.error) {
      alert(data.error);
      document.getElementById('btn-start').disabled = false;
      document.getElementById('btn-cancel').disabled = true;
      return;
    }
    log(`<span class="ts">${ts()}</span> Batch started: ${data.count} accounts, providers: ${providers.join(', ')}`, 'job-start');
    connectSSE();
    // Fetch initial status
    const statusResp = await fetch('/api/status', { headers: headers() });
    const statusData = await statusResp.json();
    renderJobs(statusData.jobs);
    updateStats(statusData.jobs);
  } catch(e) {
    alert('Failed: ' + e.message);
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-cancel').disabled = true;
  }
}

async function cancelBatch() {
  try {
    await fetch('/api/cancel', { method: 'POST', headers: headers() });
    log(`<span class="ts">${ts()}</span> Batch cancelled by user`, 'error');
  } catch(e) {}
}

function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/api/events');
  evtSource.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      handleEvent(d);
    } catch(err) {}
  };
  evtSource.onerror = () => {
    // Reconnect after 2s
    setTimeout(() => {
      if (document.getElementById('btn-start').disabled) connectSSE();
    }, 2000);
  };
}

// Track jobs locally for stats
let localJobs = [];

function handleEvent(d) {
  const t = d.type;

  if (t === 'batch_start') {
    localJobs = [];
  }

  if (t === 'job_start') {
    localJobs.push({ id: d.job_id, email: d.email, status: 'running', providers: d.providers });
    renderJobs(localJobs);
    updateStats(localJobs);
    const prov = (d.providers||[]).join('+');
    log(`<span class="ts">${ts()}</span> <span class="provider all">[${d.index+1}/${d.total}]</span> Starting <b>${d.email}</b> (${prov})`, 'job-start');
  }

  if (t === 'job_log') {
    const provCls = d.provider || '';
    const provTag = d.provider ? `<span class="provider ${provCls}">[${d.provider}]</span>` : '';
    const step = d.step ? `<b>${d.step}</b> ` : '';
    const clsMap = {error:'error', debug:'debug', stderr:'stderr', stdout:'stdout'};
    const cls = clsMap[d.log_type] || '';
    log(`<span class="ts">${ts()}</span> ${provTag} ${step}${d.message}`, cls);
  }

  if (t === 'import_ok') {
    log(`<span class="ts">${ts()}</span> <span class="provider ${d.provider}">[${d.provider}]</span> &#10004; Imported <b>${d.email}</b>`, 'import');
  }

  if (t === 'import_error') {
    log(`<span class="ts">${ts()}</span> <span class="provider ${d.provider}">[${d.provider}]</span> &#10008; Import failed: ${d.error}`, 'error');
  }

  if (t === 'job_done') {
    const j = localJobs.find(j => j.id === d.job_id);
    if (j) j.status = d.status;
    updateJobBadge(d.job_id, d.status);
    updateStats(localJobs);
    const imp = (d.imported||[]).join(', ') || 'none';
    const cls = d.status === 'success' ? 'success' : 'error';
    log(`<span class="ts">${ts()}</span> ${d.email}: <b>${d.status}</b> (imported: ${imp}, ${d.duration}s)`, cls);
  }

  if (t === 'job_error') {
    const j = localJobs.find(j => j.id === d.job_id);
    if (j) j.status = 'failed';
    updateJobBadge(d.job_id, 'failed');
    updateStats(localJobs);
    log(`<span class="ts">${ts()}</span> ${d.email}: <b>ERROR</b> ${d.error}`, 'error');
  }

  if (t === 'job_cancelled') {
    const j = localJobs.find(j => j.id === d.job_id);
    if (j) j.status = 'cancelled';
    else localJobs.push({ id: d.job_id, email: d.email, status: 'cancelled', providers: [] });
    updateJobBadge(d.job_id, 'cancelled');
    updateStats(localJobs);
  }

  if (t === 'batch_done') {
    document.getElementById('btn-start').disabled = false;
    document.getElementById('btn-cancel').disabled = true;
    log(`<span class="ts">${ts()}</span> Batch complete: ${d.success} ok, ${d.failed} failed, ${d.cancelled} cancelled`, 'batch-done');
    if (evtSource) { evtSource.close(); evtSource = null; }
  }
}

let disabledAccounts = [];
async function loadDisabled() {
  try {
    const resp = await fetch('/api/gl/disabled', { headers: headers() });
    const data = await resp.json();
    disabledAccounts = data.accounts || [];
    const el = document.getElementById('disabled-list');
    document.getElementById('disabled-count').textContent = disabledAccounts.length ? `(${disabledAccounts.length})` : '';
    if (!disabledAccounts.length) { el.innerHTML = '<span style="color:var(--muted);font-size:.8rem">No disabled accounts found</span>'; return; }
    el.innerHTML = disabledAccounts.map(a => `<div class="disabled-item"><span class="email">${a.email}</span><span class="status">DISABLED</span></div>`).join('');
  } catch(e) { alert('Failed to load: ' + e.message); }
}
async function copyDisabledEmails() {
  if (!disabledAccounts.length) { await loadDisabled(); }
  if (!disabledAccounts.length) return alert('No disabled accounts');
  const text = disabledAccounts.map(a => a.email).join('\n');
  await navigator.clipboard.writeText(text);
  log(`<span class="ts">${ts()}</span> Copied ${disabledAccounts.length} disabled emails to clipboard`, 'info');
}
async function deleteDisabledAccounts() {
  if (!disabledAccounts.length) { await loadDisabled(); }
  if (!disabledAccounts.length) return alert('No disabled accounts to delete');
  if (!confirm(`Delete ${disabledAccounts.length} disabled GL accounts? This cannot be undone.`)) return;
  try {
    const resp = await fetch('/api/gl/delete', { method: 'POST', headers: headers() });
    const data = await resp.json();
    log(`<span class="ts">${ts()}</span> ${data.message}`, 'info');
    await loadDisabled();
  } catch(e) { alert('Delete failed: ' + e.message); }
}

document.getElementById('log-viewer').addEventListener('scroll', function() {
  const el = this;
  autoScroll = (el.scrollHeight - el.scrollTop - el.clientHeight) < 50;
});
</script>
</body>
</html>
"""

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
