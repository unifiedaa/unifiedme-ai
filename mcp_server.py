#!/usr/bin/env python3
"""
MCP Server — FastMCP with Streamable HTTP transport.

Comprehensive filesystem + dev tools for Gumloop AI agents.
Workspace-scoped, path-sandboxed, configurable via CLI.

Usage:
    python mcp_server.py
    python mcp_server.py --port 9876 --workspace D:\\PROJECT\\my-project
"""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

import httpx
from fastmcp import FastMCP

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [MCP] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("mcp")

# ─── Config (set by main() before server starts) ────────────────────────────

WORKSPACE: Path = Path(".")
ALLOWED_WORKSPACES: list[Path] = []
PROXY_BASE_URL = os.environ.get("PROXY_BASE_URL", "http://127.0.0.1:1430")
PROXY_API_KEY = ""  # Set by main() from --api-key, env var, or config file

# Config file paths
_MCP_CONFIG_FILE = Path(__file__).resolve().parent / "unified" / "data" / ".mcp_api_key"
_MCP_WORKSPACES_FILE = Path(__file__).resolve().parent / "unified" / "data" / ".mcp_workspaces"


def _load_api_key(cli_key: str = "") -> str:
    """Load API key from: CLI arg > env var > config file. Returns key or empty string."""
    # 1. CLI argument (highest priority)
    if cli_key:
        return cli_key
    # 2. Environment variable
    env_key = os.environ.get("PROXY_API_KEY", "")
    if env_key:
        return env_key
    # 3. Config file
    if _MCP_CONFIG_FILE.exists():
        saved = _MCP_CONFIG_FILE.read_text().strip()
        if saved:
            return saved
    return ""


def _save_api_key(key: str) -> None:
    """Persist API key to config file for next startup."""
    try:
        _MCP_CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MCP_CONFIG_FILE.write_text(key)
    except Exception:
        pass


def _load_workspaces() -> list[str]:
    """Load allowed workspace paths from config file (one path per line)."""
    if _MCP_WORKSPACES_FILE.exists():
        try:
            lines = _MCP_WORKSPACES_FILE.read_text(encoding="utf-8").strip().splitlines()
            return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
        except Exception:
            pass
    return []


def _save_workspaces(paths: list[str]) -> None:
    """Persist workspace paths to config file."""
    try:
        _MCP_WORKSPACES_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MCP_WORKSPACES_FILE.write_text("\n".join(paths) + "\n", encoding="utf-8")
    except Exception:
        pass


def _kill_existing_on_port(port: int) -> bool:
    """Kill any process listening on the given port. Returns True if killed."""
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["netstat", "-ano"], capture_output=True, text=True, timeout=5,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid = int(parts[-1])
                    if pid > 0:
                        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                                       capture_output=True, timeout=5)
                        return True
        else:
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5,
            )
            if result.stdout.strip():
                for pid in result.stdout.strip().split("\n"):
                    subprocess.run(["kill", "-9", pid.strip()], capture_output=True, timeout=5)
                return True
    except Exception:
        pass
    return False


mcp = FastMCP(name="unified-mcp-server")


# ─── Path Safety ─────────────────────────────────────────────────────────────

_ws_file_mtime: float = 0.0


def _refresh_workspaces():
    global ALLOWED_WORKSPACES, _ws_file_mtime
    try:
        mt = _MCP_WORKSPACES_FILE.stat().st_mtime if _MCP_WORKSPACES_FILE.exists() else 0.0
    except OSError:
        return
    if mt > _ws_file_mtime:
        _ws_file_mtime = mt
        saved = _load_workspaces()
        ALLOWED_WORKSPACES = [Path(p).resolve() for p in saved if p]
        if WORKSPACE not in ALLOWED_WORKSPACES:
            ALLOWED_WORKSPACES.insert(0, WORKSPACE)


def safe_path(relative: str) -> Path:
    _refresh_workspaces()
    p = Path(relative)
    if p.is_absolute():
        resolved = p.resolve()
        if ALLOWED_WORKSPACES:
            for ws in ALLOWED_WORKSPACES:
                try:
                    resolved.relative_to(ws)
                    return resolved
                except ValueError:
                    continue
            raise ValueError(f"Path '{relative}' is not under any allowed workspace")
        return resolved
    return (WORKSPACE / relative).resolve()


def _display_path(p: Path) -> str:
    for ws in ALLOWED_WORKSPACES:
        try:
            return str(p.relative_to(ws))
        except ValueError:
            continue
    try:
        return str(_display_path(p))
    except ValueError:
        return str(p)


# ═════════════════════════════════════════════════════════════════════════════
# FILE OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def read_file(
    path: Annotated[str, "File path relative to workspace"],
    offset: Annotated[int, "Start line, 1-indexed"] = 1,
    limit: Annotated[int, "Max lines to return"] = 2000,
) -> dict:
    """Read a file from the workspace. Returns content with line numbers.
    Use offset/limit to read specific sections of large files."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    if p.is_dir():
        return {"error": f"Path is a directory: {path}"}

    offset = max(1, offset)
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return {"error": f"Read error: {e}"}

    lines = text.splitlines()
    total = len(lines)
    selected = lines[offset - 1 : offset - 1 + limit]
    numbered = [f"{offset + i}: {line}" for i, line in enumerate(selected)]

    return {
        "text": "\n".join(numbered),
        "total_lines": total,
        "showing": f"lines {offset}-{min(offset + len(selected) - 1, total)} of {total}",
    }


@mcp.tool
async def write_file(
    path: Annotated[str, "File path to write"],
    content: Annotated[str, "File content"],
) -> dict:
    """Write content to a file. Creates parent directories if needed. Overwrites existing files."""
    p = safe_path(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "path": str(_display_path(p)), "bytes": len(content.encode("utf-8"))}
    except Exception as e:
        return {"error": f"Write error: {e}"}


@mcp.tool
async def edit_file(
    path: Annotated[str, "File path to edit"],
    old_string: Annotated[str, "Text to find"],
    new_string: Annotated[str, "Replacement text"],
    replace_all: Annotated[bool, "Replace all occurrences"] = False,
) -> dict:
    """Search and replace text in a file. Fails if old_string not found or has multiple matches (unless replace_all=true)."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}
    if not old_string:
        return {"error": "old_string is required"}

    try:
        text = p.read_text(encoding="utf-8")
    except Exception as e:
        return {"error": f"Read error: {e}"}

    count = text.count(old_string)
    if count == 0:
        return {"error": f"old_string not found in {p.name}"}
    if count > 1 and not replace_all:
        return {"error": f"Found {count} matches. Set replace_all=true or provide more context."}

    if replace_all:
        new_text = text.replace(old_string, new_string)
    else:
        new_text = text.replace(old_string, new_string, 1)

    p.write_text(new_text, encoding="utf-8")
    return {"ok": True, "replacements": count if replace_all else 1, "path": str(_display_path(p))}


@mcp.tool
async def delete_file(
    path: Annotated[str, "File or directory path to delete"],
) -> dict:
    """Delete a file or empty directory from the workspace."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"Not found: {path}"}

    try:
        if p.is_dir():
            shutil.rmtree(p)
            return {"ok": True, "deleted": str(_display_path(p)), "type": "directory"}
        else:
            p.unlink()
            return {"ok": True, "deleted": str(_display_path(p)), "type": "file"}
    except Exception as e:
        return {"error": f"Delete error: {e}"}


@mcp.tool
async def rename_file(
    old_path: Annotated[str, "Current file/directory path"],
    new_path: Annotated[str, "New file/directory path"],
) -> dict:
    """Rename or move a file/directory within the workspace."""
    src = safe_path(old_path)
    dst = safe_path(new_path)

    if not src.exists():
        return {"error": f"Source not found: {old_path}"}
    if dst.exists():
        return {"error": f"Destination already exists: {new_path}"}

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        return {"ok": True, "from": old_path, "to": new_path}
    except Exception as e:
        return {"error": f"Rename error: {e}"}


@mcp.tool
async def copy_file(
    source: Annotated[str, "Source file path"],
    destination: Annotated[str, "Destination file path"],
) -> dict:
    """Copy a file within the workspace."""
    src = safe_path(source)
    dst = safe_path(destination)

    if not src.exists():
        return {"error": f"Source not found: {source}"}
    if not src.is_file():
        return {"error": f"Source is not a file: {source}"}

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return {"ok": True, "from": source, "to": destination, "bytes": dst.stat().st_size}
    except Exception as e:
        return {"error": f"Copy error: {e}"}


@mcp.tool
async def file_info(
    path: Annotated[str, "File or directory path"],
) -> dict:
    """Get detailed file/directory metadata: size, modified time, type, permissions."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"Not found: {path}"}

    try:
        stat = p.stat()
        return {
            "path": str(_display_path(p)),
            "type": "directory" if p.is_dir() else "file",
            "size_bytes": stat.st_size,
            "size_human": _human_size(stat.st_size),
            "modified": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
            "created": datetime.fromtimestamp(stat.st_ctime, tz=timezone.utc).isoformat(),
            "is_symlink": p.is_symlink(),
            "extension": p.suffix if p.is_file() else None,
        }
    except Exception as e:
        return {"error": f"Info error: {e}"}


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024  # type: ignore
    return f"{size:.1f} TB"


_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".ico", ".svg"}
_IMAGE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".tiff": "image/tiff", ".tif": "image/tiff", ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
}


@mcp.tool
async def read_image(
    path: Annotated[str, "Image file path (jpg, png, gif, webp, etc.)"],
) -> list:
    """Read an image file and return it as visual content that you can see and analyze.
    Use this to view images, screenshots, diagrams, comics, photos, etc. in the workspace.
    Returns the image so you can describe what you see."""
    import base64

    p = safe_path(path)
    if not p.exists():
        return [{"type": "text", "text": f"Error: File not found: {path}"}]
    if not p.is_file():
        return [{"type": "text", "text": f"Error: Not a file: {path}"}]

    ext = p.suffix.lower()
    if ext not in _IMAGE_EXTENSIONS:
        return [{"type": "text", "text": f"Error: Not a supported image format: {ext}. Supported: {', '.join(_IMAGE_EXTENSIONS)}"}]

    try:
        data = p.read_bytes()
        if len(data) == 0:
            return [{"type": "text", "text": "Error: Image file is empty"}]

        # Cap at 10MB
        if len(data) > 10 * 1024 * 1024:
            return [{"type": "text", "text": f"Error: Image too large ({_human_size(len(data))}). Max 10MB."}]

        mime = _IMAGE_MIME.get(ext, "image/png")
        b64 = base64.b64encode(data).decode("ascii")

        return [
            {"type": "text", "text": f"Image: {p.name} ({_human_size(len(data))}, {ext})"},
            {"type": "image", "data": b64, "mimeType": mime},
        ]

    except Exception as e:
        return [{"type": "text", "text": f"Error reading image: {e}"}]


# ═════════════════════════════════════════════════════════════════════════════
# DIRECTORY OPERATIONS
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def list_directory(
    path: Annotated[str, "Directory path, default workspace root"] = ".",
) -> dict:
    """List files and directories. Directories have trailing /. Shows [DIR] and [FILE] prefixes."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"Path not found: {path}"}
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}

    entries = []
    try:
        for item in sorted(p.iterdir()):
            prefix = "[DIR] " if item.is_dir() else "[FILE]"
            name = item.name + ("/" if item.is_dir() else "")
            entries.append(f"{prefix} {name}")
    except Exception as e:
        return {"error": f"List error: {e}"}

    return {"path": str(_display_path(p)), "entries": entries, "count": len(entries)}


@mcp.tool
async def tree(
    path: Annotated[str, "Directory path"] = ".",
    max_depth: Annotated[int, "Maximum depth to traverse"] = 3,
) -> dict:
    """Recursive directory tree with indentation. Useful for understanding project structure."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"Path not found: {path}"}
    if not p.is_dir():
        return {"error": f"Not a directory: {path}"}

    lines: list[str] = []
    _SKIP = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache", "dist", "build", ".next"}

    def _walk(dir_path: Path, prefix: str, depth: int):
        if depth > max_depth:
            lines.append(f"{prefix}...")
            return
        try:
            items = sorted(dir_path.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
        except PermissionError:
            lines.append(f"{prefix}[permission denied]")
            return

        # Filter out common noise directories
        items = [i for i in items if i.name not in _SKIP or depth == 0]

        for i, item in enumerate(items):
            is_last = i == len(items) - 1
            connector = "└── " if is_last else "├── "
            if item.is_dir():
                lines.append(f"{prefix}{connector}{item.name}/")
                extension = "    " if is_last else "│   "
                _walk(item, prefix + extension, depth + 1)
            else:
                lines.append(f"{prefix}{connector}{item.name}")

    lines.append(f"{p.name}/")
    _walk(p, "", 1)

    # Truncate if too large
    if len(lines) > 500:
        lines = lines[:500]
        lines.append(f"... (truncated, {len(lines)}+ entries)")

    return {"tree": "\n".join(lines), "root": str(_display_path(p))}


@mcp.tool
async def create_directory(
    path: Annotated[str, "Directory path to create"],
) -> dict:
    """Create a directory (and parent directories if needed)."""
    p = safe_path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "path": str(_display_path(p))}
    except Exception as e:
        return {"error": f"Mkdir error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# SEARCH
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def glob_search(
    pattern: Annotated[str, "Glob pattern, e.g. '*.py' or '**/*.html'"],
) -> dict:
    """Find files matching a glob pattern recursively in the workspace."""
    matches = []
    try:
        for p in WORKSPACE.rglob(pattern):
            rel = str(_display_path(p)).replace("\\", "/")
            if p.is_dir():
                rel += "/"
            matches.append(rel)
    except Exception as e:
        return {"error": f"Glob error: {e}"}

    matches = sorted(matches)[:200]
    return {"pattern": pattern, "matches": matches, "count": len(matches)}


@mcp.tool
async def grep(
    pattern: Annotated[str, "Regex pattern to search for"],
    include: Annotated[str, "File glob filter, e.g. '*.py'"] = "*",
    max_results: Annotated[int, "Maximum number of results"] = 100,
) -> dict:
    """Search file contents using regex pattern. Returns matching lines with file paths and line numbers."""
    max_results = min(max_results, 500)

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return {"error": f"Invalid regex: {e}"}

    results = []
    files_searched = 0
    _SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".tox", ".mypy_cache"}

    for p in WORKSPACE.rglob(include):
        # Skip binary-heavy directories
        if any(skip in p.parts for skip in _SKIP_DIRS):
            continue
        if not p.is_file():
            continue
        # Skip likely binary files
        if p.suffix in (".pyc", ".pyo", ".so", ".dll", ".exe", ".bin", ".png", ".jpg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".eot"):
            continue

        files_searched += 1
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
            for i, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    rel = str(_display_path(p)).replace("\\", "/")
                    results.append({"file": rel, "line": i, "text": line.strip()[:500]})
                    if len(results) >= max_results:
                        break
        except Exception:
            continue
        if len(results) >= max_results:
            break

    return {
        "pattern": pattern,
        "matches": results,
        "count": len(results),
        "files_searched": files_searched,
    }


# ═════════════════════════════════════════════════════════════════════════════
# SHELL / EXECUTION
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def bash(
    command: Annotated[str, "Shell command to execute"],
    timeout: Annotated[int, "Timeout in seconds (max 300)"] = 60,
) -> dict:
    """Execute a shell command in the workspace directory. Returns stdout, stderr, and exit code.
    Uses PowerShell on Windows, bash on Linux."""
    if not command:
        return {"error": "command is required"}

    timeout = min(timeout, 300)

    try:
        if os.name == "nt":
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                "bash", "-c", command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(WORKSPACE),
            )

        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        result: dict = {"exit_code": proc.returncode}
        if stdout_str:
            result["stdout"] = stdout_str[:50000]
        if stderr_str:
            result["stderr"] = stderr_str[:10000]
        return result

    except asyncio.TimeoutError:
        return {"error": f"Command timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"Exec error: {e}"}


@mcp.tool
async def run_python(
    code: Annotated[str, "Python code to execute"],
    timeout: Annotated[int, "Timeout in seconds (max 300)"] = 60,
) -> dict:
    """Execute a Python code snippet in the workspace directory. Returns stdout, stderr, and exit code.
    The code runs as a standalone script with the workspace as cwd.
    Libraries installed via pip_install are available."""
    if not code:
        return {"error": "code is required"}

    timeout = min(timeout, 300)

    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", code,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        result: dict = {"exit_code": proc.returncode}
        if stdout_str:
            result["stdout"] = stdout_str[:50000]
        if stderr_str:
            result["stderr"] = stderr_str[:10000]
        return result

    except asyncio.TimeoutError:
        return {"error": f"Python execution timed out after {timeout}s"}
    except Exception as e:
        return {"error": f"Python exec error: {e}"}


@mcp.tool
async def pip_install(
    packages: Annotated[str, "Package(s) to install, space-separated. E.g. 'requests beautifulsoup4 pandas'"],
) -> dict:
    """Install Python packages into the MCP server's environment.
    Packages become immediately available for run_python.
    Examples: 'requests', 'beautifulsoup4 lxml', 'pandas numpy matplotlib'."""
    if not packages or not packages.strip():
        return {"error": "packages is required"}

    pkg_list = packages.strip().split()
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--quiet", *pkg_list,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode == 0:
            return {"ok": True, "packages": pkg_list, "message": f"Installed: {', '.join(pkg_list)}"}
        return {"error": f"pip install failed (exit {proc.returncode}): {stderr_str[:500]}"}

    except asyncio.TimeoutError:
        return {"error": "pip install timed out (120s)"}
    except Exception as e:
        return {"error": f"pip install error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# GIT
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def git(
    command: Annotated[str, "Git command (without 'git' prefix), e.g. 'status', 'diff', 'log --oneline -10'"],
) -> dict:
    """Run a git command in the workspace. Do NOT include the 'git' prefix — just the subcommand.
    Examples: 'status', 'diff', 'log --oneline -20', 'add .', 'commit -m \"message\"'."""
    if not command:
        return {"error": "command is required"}

    full_cmd = f"git {command}"
    try:
        proc = await asyncio.create_subprocess_shell(
            full_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(WORKSPACE),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        result: dict = {"exit_code": proc.returncode}
        if stdout_str:
            result["stdout"] = stdout_str[:50000]
        if stderr_str:
            result["stderr"] = stderr_str[:10000]
        return result

    except asyncio.TimeoutError:
        return {"error": "Git command timed out after 30s"}
    except Exception as e:
        return {"error": f"Git error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# NETWORK
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def http_request(
    url: Annotated[str, "URL to request"],
    method: Annotated[str, "HTTP method: GET, POST, PUT, DELETE, PATCH"] = "GET",
    headers: Annotated[dict | None, "Optional HTTP headers"] = None,
    body: Annotated[str | None, "Optional request body (JSON string)"] = None,
    timeout: Annotated[int, "Timeout in seconds"] = 30,
) -> dict:
    """Make an HTTP request. Returns status code, headers, and response body.
    Useful for calling APIs, fetching web pages, or testing endpoints."""
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            kwargs: dict = {"method": method.upper(), "url": url}
            if headers:
                kwargs["headers"] = headers
            if body:
                kwargs["content"] = body
                if not headers or "content-type" not in {k.lower() for k in headers}:
                    kwargs.setdefault("headers", {})["Content-Type"] = "application/json"

            resp = await client.request(**kwargs)

            resp_body = resp.text[:50000]
            return {
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body": resp_body,
                "url": str(resp.url),
            }
    except Exception as e:
        return {"error": f"HTTP error: {e}"}


@mcp.tool
async def download_file(
    url: Annotated[str, "URL to download. Use gl:// for Gumloop storage, or any http/https URL."],
    filename: Annotated[str, "Save filename (auto-detected if empty)"] = "",
) -> dict:
    """Download a file from URL and save to workspace.
    For Gumloop-generated images, pass the gl:// storage_link URL directly.
    Also supports regular http/https URLs."""
    if not url:
        return {"error": "url is required"}

    if not filename:
        from urllib.parse import urlparse
        parsed = urlparse(url)
        filename = Path(parsed.path).name or "download.bin"

    save_path = safe_path(filename)

    try:
        if url.startswith("gl://"):
            # Gumloop storage — download via unified proxy (has correct account auth)
            # Re-read API key fresh from file every time
            api_key = _load_api_key()
            if not api_key or not api_key.strip() or not api_key.startswith("sk-"):
                return {
                    "error": f"gl:// download requires a valid API key (sk-xxx). "
                             f"Config file: {_MCP_CONFIG_FILE} (exists: {_MCP_CONFIG_FILE.exists()}). "
                             f"Fix: run 'unifiedme mcp apikey sk-YOUR_KEY' on the VPS, then retry."
                }
            log.info("[download] gl:// via proxy (key=%s...): %s", api_key[:8], url[:80])
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{PROXY_BASE_URL}/v1/gl-download",
                        json={"gl_url": url},
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    if resp.status_code != 200:
                        return {"error": f"Gumloop download failed: HTTP {resp.status_code} — {resp.text[:200]}"}
                    data = resp.content
            except ValueError as ve:
                return {"error": f"API key invalid or empty. Key file: {_MCP_CONFIG_FILE}. Run: unifiedme mcp apikey sk-YOUR_KEY. Detail: {ve}"}
            except Exception as he:
                return {"error": f"gl:// proxy request failed: {he}"}
        else:
            # Regular HTTP/HTTPS URL
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    return {"error": f"Download failed: HTTP {resp.status_code}"}
                data = resp.content

        if len(data) == 0:
            return {"error": "Downloaded file is empty"}

        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_bytes(data)
        log.info("[download] Saved %d bytes -> %s", len(data), save_path.name)
        return {
            "ok": True,
            "path": str(_display_path(save_path)),
            "bytes": len(data),
            "filename": filename,
        }

    except Exception as e:
        return {"error": f"Download error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# ARCHIVE
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def zip_files(
    paths: Annotated[list[str], "List of file/directory paths to include"],
    output: Annotated[str, "Output zip filename"],
) -> dict:
    """Create a zip archive from files/directories in the workspace."""
    out_path = safe_path(output)

    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        count = 0
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for p_str in paths:
                p = safe_path(p_str)
                if not p.exists():
                    return {"error": f"Not found: {p_str}"}
                if p.is_file():
                    zf.write(p, _display_path(p))
                    count += 1
                elif p.is_dir():
                    for f in p.rglob("*"):
                        if f.is_file():
                            zf.write(f, _display_path(f))
                            count += 1

        return {"ok": True, "output": output, "files_added": count, "size_bytes": out_path.stat().st_size}
    except Exception as e:
        return {"error": f"Zip error: {e}"}


@mcp.tool
async def unzip_file(
    path: Annotated[str, "Path to zip file"],
    destination: Annotated[str, "Extraction destination directory"] = ".",
) -> dict:
    """Extract a zip archive to a directory in the workspace."""
    zip_path = safe_path(path)
    dest_path = safe_path(destination)

    if not zip_path.exists():
        return {"error": f"Zip file not found: {path}"}

    try:
        dest_path.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # Validate all paths stay within workspace
            for name in zf.namelist():
                target = (dest_path / name).resolve()
                if not str(target).startswith(str(WORKSPACE.resolve())):
                    return {"error": f"Zip contains path that escapes workspace: {name}"}
            zf.extractall(dest_path)
            return {"ok": True, "destination": destination, "files_extracted": len(zf.namelist())}
    except zipfile.BadZipFile:
        return {"error": f"Not a valid zip file: {path}"}
    except Exception as e:
        return {"error": f"Unzip error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# TEXT PROCESSING
# ═════════════════════════════════════════════════════════════════════════════

@mcp.tool
async def diff(
    file_a: Annotated[str, "First file path"],
    file_b: Annotated[str, "Second file path"],
) -> dict:
    """Generate a unified diff between two files. Shows what changed between file_a and file_b."""
    pa = safe_path(file_a)
    pb = safe_path(file_b)

    if not pa.exists():
        return {"error": f"File not found: {file_a}"}
    if not pb.exists():
        return {"error": f"File not found: {file_b}"}

    try:
        lines_a = pa.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
        lines_b = pb.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

        result = list(difflib.unified_diff(
            lines_a, lines_b,
            fromfile=file_a, tofile=file_b,
            lineterm="",
        ))

        if not result:
            return {"diff": "", "message": "Files are identical"}

        return {"diff": "\n".join(result)[:50000]}
    except Exception as e:
        return {"error": f"Diff error: {e}"}


@mcp.tool
async def patch(
    path: Annotated[str, "File path to patch"],
    patch_content: Annotated[str, "Unified diff patch content to apply"],
) -> dict:
    """Apply a unified diff patch to a file. The patch should be in unified diff format.
    For simple edits, prefer edit_file instead."""
    p = safe_path(path)
    if not p.exists():
        return {"error": f"File not found: {path}"}

    try:
        original = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)

        # Parse the unified diff and apply
        # Simple approach: write patch to temp file and use subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".patch", delete=False, encoding="utf-8") as tmp:
            tmp.write(patch_content)
            tmp_path = tmp.name

        try:
            if os.name == "nt":
                # Windows: try git apply
                proc = await asyncio.create_subprocess_shell(
                    f'git apply --unsafe-paths "{tmp_path}"',
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(WORKSPACE),
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    "patch", "-p0", "-i", tmp_path,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=str(WORKSPACE),
                )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)

            if proc.returncode == 0:
                return {"ok": True, "path": path}
            else:
                return {"error": f"Patch failed: {stderr.decode('utf-8', errors='replace')}"}
        finally:
            os.unlink(tmp_path)

    except Exception as e:
        return {"error": f"Patch error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# SEARCH & RESEARCH (Context7, DuckDuckGo, grep.app)
# ═════════════════════════════════════════════════════════════════════════════

_CONTEXT7_API = "https://context7.com/api"
_CONTEXT7_KEY = os.environ.get("CONTEXT7_API_KEY", "")

_DDG_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


@mcp.tool
async def search_docs(
    library_name: Annotated[str, "Library/framework name, e.g. 'Next.js', 'React', 'FastAPI'"],
    query: Annotated[str, "What you want to know, e.g. 'how to set up middleware'"],
) -> dict:
    """Search programming library documentation via Context7. Returns up-to-date docs and code examples.
    First resolves the library, then fetches relevant documentation snippets.
    Use this when you need official docs for any library or framework."""
    try:
        c7_headers = {"X-Context7-Source": "mcp-server"}
        if _CONTEXT7_KEY:
            c7_headers["Authorization"] = f"Bearer {_CONTEXT7_KEY}"

        async with httpx.AsyncClient(timeout=30) as client:
            # Step 1: Resolve library ID
            search_resp = await client.get(
                f"{_CONTEXT7_API}/v2/libs/search",
                params={"libraryName": library_name, "query": query},
                headers=c7_headers,
            )
            if search_resp.status_code != 200:
                return {"error": f"Context7 search failed: HTTP {search_resp.status_code}"}

            results = search_resp.json().get("results", [])
            if not results:
                return {"error": f"No library found for '{library_name}'"}

            # Pick best match
            lib = results[0]
            library_id = lib.get("id", "")
            lib_info = f"{lib.get('title', '?')} ({library_id}) — {lib.get('totalSnippets', 0)} snippets"

            # Step 2: Fetch documentation
            docs_resp = await client.get(
                f"{_CONTEXT7_API}/v2/context",
                params={"libraryId": library_id, "query": query},
                headers=c7_headers,
            )
            if docs_resp.status_code != 200:
                return {"error": f"Context7 docs failed: HTTP {docs_resp.status_code}", "library": lib_info}

            docs_text = docs_resp.text[:50000]
            return {
                "library": lib_info,
                "documentation": docs_text,
            }

    except Exception as e:
        return {"error": f"search_docs error: {e}"}


@mcp.tool
async def web_search(
    query: Annotated[str, "Search query"],
    num_results: Annotated[int, "Number of results (1-20)"] = 10,
) -> dict:
    """Search the web using DuckDuckGo. No API key required.
    Returns titles, URLs, and snippets. Use this for current information, news, facts, or any web lookup."""
    if not query:
        return {"error": "query is required"}

    num_results = min(max(1, num_results), 20)

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            # Use DuckDuckGo HTML endpoint
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=_DDG_HEADERS,
            )
            if resp.status_code != 200:
                return {"error": f"DuckDuckGo returned HTTP {resp.status_code}"}

            html = resp.text
            results = _parse_ddg_html(html, num_results)

            if not results:
                return {"query": query, "results": [], "message": "No results found"}

            return {"query": query, "results": results, "count": len(results)}

    except Exception as e:
        return {"error": f"web_search error: {e}"}


def _parse_ddg_html(html: str, max_results: int) -> list[dict]:
    """Parse DuckDuckGo HTML search results."""
    from urllib.parse import unquote
    import html as html_mod

    results = []

    # Find all result__a links and result__snippet texts
    links = re.findall(
        r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    )
    snippets = re.findall(
        r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL
    )

    for i, (raw_url, raw_title) in enumerate(links):
        # Skip ads (contain ad_provider or ad_domain in URL)
        if "ad_provider" in raw_url or "ad_domain" in raw_url:
            continue

        # Clean title
        title = re.sub(r'<[^>]+>', '', raw_title).strip()
        title = html_mod.unescape(title)

        # Extract actual URL from DDG redirect: //duckduckgo.com/l/?uddg=ENCODED_URL&...
        actual_url = raw_url
        uddg_match = re.search(r'uddg=([^&]+)', raw_url)
        if uddg_match:
            actual_url = unquote(html_mod.unescape(uddg_match.group(1)))

        # Clean snippet
        snippet = ""
        if i < len(snippets):
            snippet = re.sub(r'<[^>]+>', '', snippets[i]).strip()
            snippet = html_mod.unescape(snippet)

        if title and actual_url and not actual_url.startswith("//duckduckgo.com"):
            results.append({"title": title, "url": actual_url, "snippet": snippet})

        if len(results) >= max_results:
            break

    return results


@mcp.tool
async def fetch_url(
    url: Annotated[str, "URL to fetch"],
    max_length: Annotated[int, "Max characters to return"] = 20000,
) -> dict:
    """Fetch a web page and extract its text content. Strips HTML tags, scripts, styles, and navigation.
    Returns clean readable text. Use this to read articles, documentation pages, or any web content."""
    if not url:
        return {"error": "url is required"}

    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            resp = await client.get(url, headers=_DDG_HEADERS)
            if resp.status_code != 200:
                return {"error": f"HTTP {resp.status_code}", "url": url}

            html = resp.text

            # Strip script and style tags entirely
            html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<style[^>]*>.*?</style>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<nav[^>]*>.*?</nav>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<header[^>]*>.*?</header>', '', html, flags=re.DOTALL | re.IGNORECASE)
            html = re.sub(r'<footer[^>]*>.*?</footer>', '', html, flags=re.DOTALL | re.IGNORECASE)

            # Convert common block elements to newlines
            html = re.sub(r'<br\s*/?>', '\n', html, flags=re.IGNORECASE)
            html = re.sub(r'</(p|div|h[1-6]|li|tr|blockquote)>', '\n', html, flags=re.IGNORECASE)

            # Strip remaining HTML tags
            text = re.sub(r'<[^>]+>', '', html)

            # Decode HTML entities
            import html as html_mod
            text = html_mod.unescape(text)

            # Clean up whitespace
            lines = [line.strip() for line in text.splitlines()]
            lines = [line for line in lines if line]
            text = "\n".join(lines)

            # Truncate
            if len(text) > max_length:
                text = text[:max_length] + "\n\n[... truncated]"

            return {
                "url": str(resp.url),
                "content": text,
                "length": len(text),
            }

    except Exception as e:
        return {"error": f"fetch_url error: {e}"}


@mcp.tool
async def search_github_code(
    query: Annotated[str, "Code pattern to search for (literal string or regex)"],
    language: Annotated[str | None, "Programming language filter, e.g. 'Python', 'TypeScript'"] = None,
    repo: Annotated[str | None, "Repository filter, e.g. 'facebook/react'"] = None,
    path: Annotated[str | None, "File path filter, e.g. 'src/'"] = None,
    use_regex: Annotated[bool, "Treat query as regex pattern"] = False,
) -> dict:
    """Search code across millions of public GitHub repositories using grep.app.
    No API key required. Returns matching code snippets with file paths and repo info.
    Use this to find real-world code examples, usage patterns, or implementations."""
    if not query:
        return {"error": "query is required"}

    try:
        params: dict = {"q": query, "format": "json"}
        if use_regex:
            params["regexp"] = "true"
        if language:
            params["filter[lang][0]"] = language
        if repo:
            params["filter[repo][0]"] = repo
        if path:
            params["filter[path][0]"] = path

        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(
                "https://grep.app/api/search",
                params=params,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                },
            )
            if resp.status_code != 200:
                return {"error": f"grep.app returned HTTP {resp.status_code}"}

            data = resp.json()
            hits = data.get("hits", {}).get("hits", [])

            results = []
            for hit in hits[:20]:
                snippet = hit.get("content", {}).get("snippet", "")
                # Clean HTML from snippet
                snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()

                # grep.app returns repo/path as strings or dicts depending on version
                repo_val = hit.get("repo", "")
                if isinstance(repo_val, dict):
                    repo_val = repo_val.get("raw", "")
                path_val = hit.get("path", "")
                if isinstance(path_val, dict):
                    path_val = path_val.get("raw", "")

                results.append({
                    "repo": repo_val,
                    "file": path_val,
                    "lines": snippet_clean[:500],
                    "language": hit.get("lang", ""),
                })

            total_raw = data.get("hits", {}).get("total", 0)
            total = total_raw.get("value", 0) if isinstance(total_raw, dict) else int(total_raw or 0)
            return {
                "query": query,
                "results": results,
                "count": len(results),
                "total_matches": total,
            }

    except Exception as e:
        return {"error": f"search_github_code error: {e}"}


# ═════════════════════════════════════════════════════════════════════════════
# MAIN
# ═════════════════════════════════════════════════════════════════════════════

def _check_cloudflared() -> str | None:
    """Check if cloudflared is installed. Returns path or None."""
    import shutil as _shutil
    return _shutil.which("cloudflared")


def _start_tunnel(port: int) -> tuple[subprocess.Popen | None, str]:
    """Start cloudflared tunnel in background. Returns (process, tunnel_url)."""
    cf_path = _check_cloudflared()
    if not cf_path:
        return None, ""

    # Use PIPE for stderr so we can read without file locking issues on Windows
    proc = subprocess.Popen(
        [cf_path, "tunnel", "--url", f"http://localhost:{port}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    # Read stderr lines until we find the tunnel URL (max 20 seconds)
    import time
    import threading

    tunnel_url = ""
    lines_buf: list[str] = []

    def _read_stderr():
        nonlocal tunnel_url
        try:
            for raw_line in proc.stderr:  # type: ignore
                line = raw_line.decode("utf-8", errors="replace")
                lines_buf.append(line)
                match = re.search(r'(https://[a-z0-9\-]+\.trycloudflare\.com)', line)
                if match:
                    tunnel_url = match.group(1)
                    break
        except Exception:
            pass

    reader = threading.Thread(target=_read_stderr, daemon=True)
    reader.start()
    reader.join(timeout=20)

    return proc, tunnel_url


def _start_named_tunnel(tunnel_name: str, port: int) -> tuple[subprocess.Popen | None, str]:
    """Start a cloudflared named tunnel. Returns (process, configured_url).

    Named tunnels have a fixed DNS route — the URL doesn't change between restarts.
    Requires prior setup: cloudflared tunnel login + create + route dns.
    """
    cf_path = _check_cloudflared()
    if not cf_path:
        return None, ""

    _cf_config_dir = Path.home() / ".cloudflared"
    config_file = _cf_config_dir / "config.yml"

    cmd = [cf_path, "tunnel", "run"]
    if config_file.exists():
        cmd.extend(["--config", str(config_file)])
    cmd.append(tunnel_name)

    try:
        if os.name == "nt":
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )

        import time as _time
        _time.sleep(3)

        if proc.poll() is not None:
            stderr_out = proc.stderr.read().decode("utf-8", errors="replace")[:500] if proc.stderr else ""
            log.error("Named tunnel '%s' exited immediately: %s", tunnel_name, stderr_out)
            return None, ""

        tunnel_url = _read_named_tunnel_url(tunnel_name)
        return proc, tunnel_url

    except Exception as e:
        log.error("Failed to start named tunnel '%s': %s", tunnel_name, e)
        return None, ""


def _read_named_tunnel_url(tunnel_name: str) -> str:
    """Read the configured hostname for a named tunnel from config.yml or tunnel info."""
    _cf_config_dir = Path.home() / ".cloudflared"
    config_file = _cf_config_dir / "config.yml"

    if config_file.exists():
        try:
            content = config_file.read_text(encoding="utf-8")
            match = re.search(r"hostname:\s*(\S+)", content)
            if match:
                hostname = match.group(1)
                if not hostname.startswith("http"):
                    hostname = f"https://{hostname}"
                return hostname
        except Exception:
            pass

    return f"https://{tunnel_name}.your-domain.com"


def _interactive_setup(args) -> tuple[str, str, int]:
    """Interactive CLI setup. Returns (workspace, api_key, port)."""
    print()
    print("  +==========================================+")
    print("  |        MCP Server — Interactive Setup    |")
    print("  +==========================================+")
    print()

    # ── API Key ──
    saved_key = _load_api_key(args.api_key)
    if saved_key:
        masked = saved_key[:8] + "..." + saved_key[-4:]
        print(f"  API Key:  {masked} (saved)")
        try:
            change = input("  Change API key? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        if change == "y":
            try:
                new_key = input("  Enter new API key (sk-xxx): ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(0)
            if new_key:
                saved_key = new_key
    else:
        print("  No API key found. Needed for gl:// image downloads.")
        print("  Get it from: http://localhost:1430/dashboard → API Keys")
        print()
        try:
            saved_key = input("  Enter API key (sk-xxx), or press Enter to skip: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

    if saved_key:
        _save_api_key(saved_key)

    print()

    # ── Workspace ──
    default_ws = args.workspace
    print(f"  Workspace: folder that MCP tools can read/write.")
    try:
        ws_input = input(f"  Enter workspace path [{default_ws}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    workspace = ws_input if ws_input else default_ws

    print()

    # ── Port ──
    port = args.port
    try:
        port_input = input(f"  Port [{port}]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    if port_input.isdigit():
        port = int(port_input)

    return workspace, saved_key, port


def main():
    global WORKSPACE, ALLOWED_WORKSPACES, PROXY_API_KEY

    parser = argparse.ArgumentParser(description="MCP Server (FastMCP + Streamable HTTP)")
    parser.add_argument("--port", type=int, default=9876, help="Listen port (default: 9876)")
    parser.add_argument("--workspace", type=str, default="", help="Primary workspace directory")
    parser.add_argument("--api-key", type=str, default="", help="Unified proxy API key (sk-xxx)")
    parser.add_argument("--no-tunnel", action="store_true", help="Don't start cloudflared tunnel")
    parser.add_argument("--no-interactive", action="store_true", help="Skip interactive setup")
    parser.add_argument("--named-tunnel", type=str, default="", help="Use cloudflared named tunnel (persistent URL)")
    args = parser.parse_args()

    # ── Interactive or direct mode ──
    if not args.no_interactive and not args.workspace:
        workspace, api_key, port = _interactive_setup(args)
    else:
        workspace = args.workspace or os.getcwd()
        api_key = _load_api_key(args.api_key)
        port = args.port
        if api_key:
            _save_api_key(api_key)

    # ── Auto-kill existing MCP server on same port ──
    if _kill_existing_on_port(port):
        log.info("Killed existing process on port %d", port)
        import time as _t
        _t.sleep(1)

    WORKSPACE = Path(workspace).resolve()
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    PROXY_API_KEY = api_key

    # ── Load allowed workspaces from config ──
    saved_workspaces = _load_workspaces()
    ALLOWED_WORKSPACES = [Path(p).resolve() for p in saved_workspaces if p]
    if WORKSPACE not in ALLOWED_WORKSPACES:
        ALLOWED_WORKSPACES.insert(0, WORKSPACE)

    if PROXY_API_KEY:
        key_display = PROXY_API_KEY[:8] + "..." + PROXY_API_KEY[-4:]
    else:
        key_display = "(not set)"

    # ── Tool list ──
    tool_names = [
        "read_file", "write_file", "edit_file", "delete_file", "rename_file",
        "copy_file", "file_info", "read_image", "list_directory", "tree", "create_directory",
        "glob_search", "grep", "bash", "run_python", "pip_install", "git",
        "http_request", "download_file", "zip_files", "unzip_file", "diff", "patch",
        "search_docs", "web_search", "fetch_url", "search_github_code",
    ]

    # ── Start cloudflared tunnel ──
    tunnel_proc = None
    tunnel_url = ""
    if args.named_tunnel:
        cf_path = _check_cloudflared()
        if cf_path:
            print()
            print(f"  Starting named tunnel '{args.named_tunnel}'...", end=" ", flush=True)
            tunnel_proc, tunnel_url = _start_named_tunnel(args.named_tunnel, port)
            if tunnel_proc:
                print("OK")
            else:
                print("FAILED")
        else:
            print()
            print("  cloudflared not found — install it or use --no-tunnel")
    elif not args.no_tunnel:
        cf_path = _check_cloudflared()
        if cf_path:
            print()
            print("  Starting cloudflared tunnel...", end=" ", flush=True)
            tunnel_proc, tunnel_url = _start_tunnel(port)
            if tunnel_url:
                print("OK")
            else:
                print("FAILED (timeout — start manually)")
        else:
            print()
            print("  cloudflared not found — install it or use --no-tunnel")

    # ── Banner ──
    print()
    print("=" * 60)
    print(f"  MCP Server (FastMCP) — {len(tool_names)} tools")
    print(f"  Port:      {port}")
    print(f"  Workspace: {WORKSPACE}")
    if len(ALLOWED_WORKSPACES) > 1:
        print(f"  Allowed:   {len(ALLOWED_WORKSPACES)} paths")
        for ws in ALLOWED_WORKSPACES:
            print(f"             - {ws}")
    print(f"  API Key:   {key_display}")
    if tunnel_url:
        print(f"  Tunnel:    {tunnel_url}")
        print(f"  MCP URL:   {tunnel_url}/mcp")
    else:
        print(f"  Endpoint:  http://0.0.0.0:{port}/mcp")
    print("=" * 60)
    print()
    print("  Tools:")
    for i in range(0, len(tool_names), 4):
        row = ", ".join(tool_names[i:i + 4])
        print(f"    {row}")
    print()

    if tunnel_url:
        print(f"  >>> Add this URL to Gumloop MCP settings:")
        print(f"  >>> {tunnel_url}/mcp")
        print()

    print("  Press Ctrl+C to stop.")
    print()

    try:
        mcp.run(transport="http", host="0.0.0.0", port=port)
    except KeyboardInterrupt:
        print("\n  Shutting down...")
    finally:
        if tunnel_proc:
            tunnel_proc.terminate()
            try:
                tunnel_proc.wait(timeout=5)
            except Exception:
                tunnel_proc.kill()
            print("  Tunnel stopped.")
        print("  Bye.")


if __name__ == "__main__":
    main()
