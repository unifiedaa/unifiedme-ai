"""Built-in local tool executor for proxy-native tool loop.

Executes read/grep/glob/bash/edit/write locally and returns compact results.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any

log = logging.getLogger("unified.loop_tools")

MAX_RESULT_CHARS = 8192
MAX_READ_LINES = 200
MAX_READ_TAIL = 20
MAX_GREP_MATCHES = 50
MAX_GLOB_ENTRIES = 100
BASH_TIMEOUT = 30
BASH_MAX_OUTPUT = 65536

BLOCKED_BASH_PATTERNS = [
    re.compile(r"rm\s+-rf\s+/(?!tmp)", re.IGNORECASE),
    re.compile(r"\bsudo\b"),
    re.compile(r"\bchmod\s+777\b"),
    re.compile(r">(>)?\s*/etc/"),
    re.compile(r"\bshutdown\b"),
    re.compile(r"\breboot\b"),
    re.compile(r"\bformat\b.*[A-Z]:", re.IGNORECASE),
]


def _truncate(text: str, max_chars: int = MAX_RESULT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[truncated at {max_chars} chars]"


async def execute_tool(name: str, args: dict[str, Any]) -> str:
    """Execute a tool and return compact result string."""
    try:
        handler = _TOOL_HANDLERS.get(name)
        if not handler:
            return f"ERROR: Unknown tool '{name}'"
        result = await handler(args)
        return _truncate(result)
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


async def _tool_read(args: dict[str, Any]) -> str:
    file_path = args.get("filePath", "")
    if not file_path:
        return "ERROR: filePath required"

    p = Path(file_path)
    if not p.exists():
        return f"ERROR: File not found: {file_path}"

    if p.is_dir():
        entries = []
        try:
            for item in sorted(p.iterdir()):
                suffix = "/" if item.is_dir() else ""
                entries.append(f"{item.name}{suffix}")
                if len(entries) >= MAX_GLOB_ENTRIES:
                    entries.append(f"[... truncated, {len(list(p.iterdir()))} total entries]")
                    break
        except PermissionError:
            return f"ERROR: Permission denied: {file_path}"
        return "\n".join(entries)

    offset = int(args.get("offset", 1)) - 1
    limit = int(args.get("limit", MAX_READ_LINES))
    limit = min(limit, MAX_READ_LINES)

    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
    except PermissionError:
        return f"ERROR: Permission denied: {file_path}"

    total = len(lines)
    selected = lines[offset:offset + limit]

    if total > offset + limit and total > MAX_READ_LINES + MAX_READ_TAIL:
        tail = lines[-MAX_READ_TAIL:]
        result_lines = []
        for i, line in enumerate(selected, start=offset + 1):
            result_lines.append(f"{i}: {line.rstrip()}")
        result_lines.append(f"\n[... {total - offset - limit - MAX_READ_TAIL} lines omitted ...]")
        for i, line in enumerate(tail, start=total - MAX_READ_TAIL + 1):
            result_lines.append(f"{i}: {line.rstrip()}")
        return "\n".join(result_lines)

    result_lines = []
    for i, line in enumerate(selected, start=offset + 1):
        result_lines.append(f"{i}: {line.rstrip()}")

    header = f"[{file_path}] ({total} lines total, showing {offset+1}-{min(offset+limit, total)})"
    return header + "\n" + "\n".join(result_lines)


async def _tool_grep(args: dict[str, Any]) -> str:
    pattern = args.get("pattern", "")
    path = args.get("path", ".")
    include = args.get("include", "")

    if not pattern:
        return "ERROR: pattern required"

    try:
        regex = re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        return f"ERROR: Invalid regex: {e}"

    search_path = Path(path)
    if not search_path.exists():
        return f"ERROR: Path not found: {path}"

    matches = []
    files_searched = 0

    def _search_file(fp: Path):
        nonlocal files_searched
        files_searched += 1
        try:
            with open(fp, "r", encoding="utf-8", errors="replace") as f:
                for line_num, line in enumerate(f, 1):
                    if regex.search(line):
                        matches.append(f"{fp}:{line_num}: {line.rstrip()}")
                        if len(matches) >= MAX_GREP_MATCHES:
                            return
        except (PermissionError, OSError):
            pass

    if search_path.is_file():
        _search_file(search_path)
    else:
        glob_pattern = include or "**/*"
        for fp in search_path.glob(glob_pattern):
            if fp.is_file() and fp.stat().st_size < 1_000_000:
                _search_file(fp)
                if len(matches) >= MAX_GREP_MATCHES:
                    break

    if not matches:
        return f"No matches for '{pattern}' in {path} ({files_searched} files searched)"

    header = f"Found {len(matches)} match(es) ({files_searched} files searched)"
    if len(matches) >= MAX_GREP_MATCHES:
        header += f" [capped at {MAX_GREP_MATCHES}]"
    return header + "\n" + "\n".join(matches)


async def _tool_glob(args: dict[str, Any]) -> str:
    pattern = args.get("pattern", "**/*")
    path = args.get("path", ".")

    search_path = Path(path)
    if not search_path.exists():
        return f"ERROR: Path not found: {path}"

    entries = []
    for fp in search_path.glob(pattern):
        suffix = "/" if fp.is_dir() else ""
        entries.append(f"{fp}{suffix}")
        if len(entries) >= MAX_GLOB_ENTRIES:
            entries.append(f"[... capped at {MAX_GLOB_ENTRIES}]")
            break

    if not entries:
        return f"No files matching '{pattern}' in {path}"

    return f"Found {len(entries)} file(s)\n" + "\n".join(entries)


async def _tool_bash(args: dict[str, Any]) -> str:
    command = args.get("command", "")
    if not command:
        return "ERROR: command required"

    for blocked in BLOCKED_BASH_PATTERNS:
        if blocked.search(command):
            return f"ERROR: Command blocked by safety filter"

    workdir = args.get("workdir", None)
    if workdir and not Path(workdir).is_dir():
        return f"ERROR: workdir not found: {workdir}"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=workdir,
            env={**os.environ},
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=BASH_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            return f"ERROR: Command timed out after {BASH_TIMEOUT}s"

        stdout_str = stdout.decode("utf-8", errors="replace")[:BASH_MAX_OUTPUT]
        stderr_str = stderr.decode("utf-8", errors="replace")[:4096]

        parts = []
        if stdout_str:
            parts.append(stdout_str)
        if stderr_str:
            parts.append(f"[stderr]\n{stderr_str}")
        parts.append(f"[exit code: {proc.returncode}]")

        return "\n".join(parts)

    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


async def _tool_write(args: dict[str, Any]) -> str:
    file_path = args.get("filePath", "")
    content = args.get("content", "")

    if not file_path:
        return "ERROR: filePath required"

    p = Path(file_path)
    p.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        lines = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
        return f"OK: wrote {file_path} ({lines} lines)"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


async def _tool_edit(args: dict[str, Any]) -> str:
    file_path = args.get("filePath", "")
    old_string = args.get("oldString", "")
    new_string = args.get("newString", "")

    if not file_path:
        return "ERROR: filePath required"
    if not old_string:
        return "ERROR: oldString required"

    p = Path(file_path)
    if not p.exists():
        return f"ERROR: File not found: {file_path}"

    try:
        content = p.read_text(encoding="utf-8")
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"

    count = content.count(old_string)
    if count == 0:
        return "ERROR: oldString not found in file"

    replace_all = args.get("replaceAll", False)
    if count > 1 and not replace_all:
        return f"ERROR: Found {count} matches. Use replaceAll=true or provide more context."

    if replace_all:
        new_content = content.replace(old_string, new_string)
    else:
        new_content = content.replace(old_string, new_string, 1)

    try:
        p.write_text(new_content, encoding="utf-8")
        return f"OK: edited {file_path} ({count} replacement{'s' if count > 1 else ''})"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


_TOOL_HANDLERS = {
    "read": _tool_read,
    "grep": _tool_grep,
    "glob": _tool_glob,
    "bash": _tool_bash,
    "write": _tool_write,
    "edit": _tool_edit,
}

SUPPORTED_TOOLS = set(_TOOL_HANDLERS.keys())
