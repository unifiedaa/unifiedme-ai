from __future__ import annotations

import asyncio
import importlib.util
import logging
import os
import platform
import shutil
import subprocess
import sys
import time

import httpx

from .config import BASE_DIR, DATA_DIR

log = logging.getLogger("unified.solver_manager")


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            return str(pid) in result.stdout
        os.kill(pid, 0)
        return True
    except (OSError, subprocess.TimeoutExpired):
        return False


def _kill_pid(pid: int) -> None:
    try:
        if os.name == "nt":
            _ = subprocess.run(
                ["taskkill", "/F", "/PID", str(pid), "/T"],
                capture_output=True,
                timeout=10,
            )
        else:
            os.kill(pid, 15)
            time.sleep(2)
            try:
                os.kill(pid, 0)
                os.kill(pid, 9)
            except OSError:
                pass
    except Exception as exc:
        log.warning("Failed to kill solver PID %d: %s", pid, exc)


def _kill_port(port: int) -> None:
    try:
        if os.name == "nt":
            result = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
            for line in result.stdout.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.strip().split()
                    pid = int(parts[-1])
                    if pid > 0:
                        log.info("Killing existing process on solver port %d (PID %d)", port, pid)
                        _ = subprocess.run(
                            ["taskkill", "/F", "/PID", str(pid), "/T"],
                            capture_output=True,
                            timeout=10,
                        )
        else:
            result = subprocess.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5)
            for pid_str in result.stdout.strip().splitlines():
                pid = int(pid_str.strip())
                if pid > 0:
                    log.info("Killing existing process on solver port %d (PID %d)", port, pid)
                    _kill_pid(pid)
    except Exception as exc:
        log.debug("kill_port(%d) non-fatal error: %s", port, exc)


class SolverManager:
    PORT: int = int(os.getenv("CAPTCHA_SOLVER_PORT", "5000"))
    BASE_URL: str = f"http://127.0.0.1:{PORT}"
    PID_FILE = DATA_DIR / ".solver_pid"
    LOG_FILE = DATA_DIR / "captcha_solver.log"

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._pid: int | None = None

    def check_deps(self) -> dict[str, bool]:
        return {
            "quart": importlib.util.find_spec("quart") is not None,
            "camoufox": importlib.util.find_spec("camoufox") is not None,
            "xvfb_run": shutil.which("xvfb-run") is not None,
        }

    def is_available(self) -> bool:
        deps = self.check_deps()
        return deps["quart"] and deps["camoufox"]

    def _read_pid_file(self) -> int | None:
        try:
            if self.PID_FILE.exists():
                return int(self.PID_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            pass
        return None

    def _write_pid_file(self, pid: int) -> None:
        self.PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        _ = self.PID_FILE.write_text(str(pid), encoding="utf-8")

    def _clear_pid_file(self) -> None:
        try:
            self.PID_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    def is_running(self) -> bool:
        pid = self._pid or self._read_pid_file()
        if pid and _is_pid_alive(pid):
            self._pid = pid
            return True
        self._pid = None
        self._clear_pid_file()
        return False

    async def _wait_until_ready(self) -> bool:
        for _ in range(30):
            await asyncio.sleep(1)
            if not self.is_running():
                return False
            try:
                async with httpx.AsyncClient(timeout=5) as client:
                    resp = await client.post(f"{self.BASE_URL}/turnstile", json={})
                    if resp.status_code in {400, 422}:
                        return True
            except Exception:
                pass
        return False

    async def start(self) -> bool:
        if self.is_running():
            if await self._wait_until_ready():
                return True

        if not self.is_available():
            return False

        _kill_port(self.PORT)
        if self._pid and _is_pid_alive(self._pid):
            _kill_pid(self._pid)
        stale_pid = self._read_pid_file()
        if stale_pid and _is_pid_alive(stale_pid):
            _kill_pid(stale_pid)
        await asyncio.sleep(1)

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "CAPTCHA_SOLVER_PORT": str(self.PORT),
        }
        cmd = [sys.executable, "-m", "unified.captcha_solver"]
        if platform.system() == "Linux" and not os.getenv("DISPLAY") and shutil.which("xvfb-run"):
            cmd = ["xvfb-run", "-a", *cmd]

        log_fh = open(self.LOG_FILE, "a", encoding="utf-8")
        _ = log_fh.write(f"\n{'=' * 60}\n")
        _ = log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Starting captcha solver\n")
        _ = log_fh.write(f"  Port: {self.PORT}\n")
        _ = log_fh.write(f"  Cmd:  {' '.join(cmd)}\n")
        _ = log_fh.write(f"{'=' * 60}\n")
        log_fh.flush()

        try:
            if platform.system() == "Windows":
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(BASE_DIR.parent),
                    env=env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                self._proc = subprocess.Popen(
                    cmd,
                    cwd=str(BASE_DIR.parent),
                    env=env,
                    stdout=log_fh,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
        except Exception as exc:
            log.error("Failed to spawn captcha solver: %s", exc)
            log_fh.close()
            return False

        self._pid = self._proc.pid
        self._write_pid_file(self._pid)
        log.info("Captcha solver spawned (PID %d, port %d)", self._pid, self.PORT)

        ready = await self._wait_until_ready()
        if not ready:
            log.error("Captcha solver did not become ready within 30s")
            await self.stop()
            return False
        return True

    async def stop(self) -> None:
        pid = self._pid or self._read_pid_file()
        if pid and _is_pid_alive(pid):
            _kill_pid(pid)
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
        _kill_port(self.PORT)
        self._proc = None
        self._pid = None
        self._clear_pid_file()
        log.info("Captcha solver stopped")

    async def solve_token(self, sitekey: str, url: str, action: str = "") -> str:
        if not self.is_running():
            return ""

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.BASE_URL}/turnstile",
                    json={
                        "sitekey": sitekey,
                        "url": url,
                        "action": action,
                    },
                )
                if resp.status_code not in {200, 202}:
                    log.warning("Captcha solver task creation failed: HTTP %d", resp.status_code)
                    return ""
                response_data = resp.json()
                task_id = response_data.get("task_id", "") if isinstance(response_data, dict) else ""
                if not task_id:
                    return ""

                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    await asyncio.sleep(2)
                    result_resp = await client.get(f"{self.BASE_URL}/result", params={"id": task_id})
                    if result_resp.status_code == 202:
                        continue
                    if result_resp.status_code != 200:
                        log.warning("Captcha solver task %s failed: HTTP %d", task_id, result_resp.status_code)
                        return ""
                    data = result_resp.json()
                    if not isinstance(data, dict):
                        return ""
                    if data.get("status") == "success":
                        result_data = data.get("data", {})
                        if isinstance(result_data, dict):
                            token = result_data.get("token", "")
                            return token if isinstance(token, str) else ""
                        return ""
                    if data.get("status") == "error":
                        return ""
        except Exception as exc:
            log.warning("Captcha solver request error: %s", exc)
        return ""


solver_manager = SolverManager()
