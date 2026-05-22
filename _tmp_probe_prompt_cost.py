import json
import re
import sqlite3
from pathlib import Path
from typing import Any


DB_PATH = Path(r"C:\Users\User\unifiedme-ai\unified\data\unified.db")
TARGET_PATH = Path(r"C:\Users\User\Documents\SEMUA DISINI-2025\KERJA\RDP\camoufox-rdp-manager")
SESSION_ID = 42
RECENT_WINDOW = 10


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _extract_key_signals(text: str) -> list[str]:
    signals: list[str] = []
    file_paths = re.findall(r"[\w./\\-]+\.\w{1,10}", text)
    for fp in file_paths[:5]:
        if len(fp) > 4:
            signals.append(fp)
    decisions = re.findall(
        r"(?:decided|chose|will use|switched to|implemented|created|fixed|added|removed|updated)\s+[^.!?\n]{5,80}",
        text,
        re.IGNORECASE,
    )
    for d in decisions[:3]:
        signals.append(d.strip())
    return signals


def compact_messages(messages: list[dict[str, Any]], max_chars: int = 4000) -> str:
    if not messages:
        return ""
    total = len(messages)
    user_queries: list[str] = []
    assistant_actions: list[str] = []
    key_signals: list[str] = []
    for msg in messages:
        role = msg.get("role", "")
        text = str(msg.get("content", "") or "")
        if not text.strip():
            continue
        key_signals.extend(_extract_key_signals(text))
        if role == "user":
            user_queries.append(_truncate(text.strip().split("\n")[0], 300))
        elif role == "assistant":
            assistant_actions.append(_truncate(text.strip().split("\n")[0], 300))
    unique_signals = list(dict.fromkeys(key_signals))[:15]
    lines: list[str] = [f"[Session summary: {total} messages compacted]"]
    if unique_signals:
        lines.append(f"Key artifacts: {', '.join(unique_signals[:10])}")
    if user_queries:
        lines.append(f"User topics ({len(user_queries)} queries):")
        for q in user_queries[:8]:
            lines.append(f"  - {q}")
    if assistant_actions:
        lines.append(f"Assistant actions ({len(assistant_actions)} responses):")
        for a in assistant_actions[:8]:
            lines.append(f"  - {a}")
    result = "\n".join(lines)
    return result if len(result) <= max_chars else result[: max_chars - 3] + "..."


def tools_to_system_prompt(tools: list[dict[str, Any]]) -> str:
    if not tools:
        return ""
    lines = ["You have access to the following tools:\n"]
    for tool in tools:
        lines.append(f"<tool name=\"{tool.get('name','')}\">")
        if tool.get("description"):
            lines.append(f"<description>{tool['description']}</description>")
        schema = tool.get("input_schema") or {}
        if schema:
            lines.append(f"<parameters>{json.dumps(schema, ensure_ascii=False)}</parameters>")
        lines.append("</tool>\n")
    lines.append(
        "When you need to use a tool, output it in this exact format:\n"
        "<tool_use>\n<name>tool_name</name>\n<input>{\"param\": \"value\"}</input>\n</tool_use>"
    )
    return "\n".join(lines)


def chars(obj) -> int:
    return len(json.dumps(obj, ensure_ascii=False))


def load_messages(session_id: int) -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT role, content FROM chat_messages WHERE session_id = ? ORDER BY id ASC",
        (session_id,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def persisted_rows_to_openai_messages(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for row in rows:
        role = row.get("role", "")
        if role not in ("system", "user", "assistant"):
            continue
        result.append({"role": role, "content": row.get("content", "")})
    return result


def trim_toolish_text(text: str, max_len: int = 400) -> str:
    if not isinstance(text, str):
        return str(text)
    markers = ["> **[Tool]**", "> **[Result]**", "<tool_use", "<tool_result"]
    if any(m in text for m in markers):
        return text[:max_len] + ("..." if len(text) > max_len else "")
    return text


def trim_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for msg in messages:
        out.append({"role": msg.get("role", "user"), "content": trim_toolish_text(msg.get("content", ""))})
    return out


def build_current_style_context(stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary_text = compact_messages(stored)
    recent = stored[-RECENT_WINDOW:]
    parts = [
        "You are continuing a conversation from the same OpenCode session but with a different upstream account.",
        "Below is a compacted summary of prior conversation followed by recent messages.",
        "",
        "<session_summary>",
        summary_text,
        "</session_summary>",
        "",
        "<recent_messages>",
    ]
    for msg in recent:
        parts.append(f"{str(msg.get('role', '')).upper()}: {msg.get('content', '')}")
    parts.append("</recent_messages>")
    return [{"role": "system", "content": "\n".join(parts)}]


def build_trimmed_context(stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    trimmed = trim_messages(stored)
    summary_text = compact_messages(trimmed, max_chars=1800)
    recent = trimmed[-6:]
    parts = [
        "Continue same OpenCode session. Prior context summary + recent relevant messages below.",
        "<session_summary>",
        summary_text,
        "</session_summary>",
        "<recent_messages>",
    ]
    for msg in recent:
        parts.append(f"{str(msg.get('role', '')).upper()}: {msg.get('content', '')}")
    parts.append("</recent_messages>")
    return [{"role": "system", "content": "\n".join(parts)}]


def sample_tools() -> list[dict[str, Any]]:
    return [
        {
            "name": "read",
            "description": "Read a file or directory",
            "input_schema": {"type": "object", "properties": {"filePath": {"type": "string"}}, "required": ["filePath"]},
        },
        {
            "name": "grep",
            "description": "Search file contents by regex",
            "input_schema": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"}}},
        },
        {
            "name": "bash",
            "description": "Run a shell command",
            "input_schema": {"type": "object", "properties": {"command": {"type": "string"}, "description": {"type": "string"}}},
        },
    ]


def load_codebase_snapshot(root: Path, max_file_chars: int | None = None) -> str:
    parts = [f"# Codebase snapshot: {root}", "", "## Tree"]
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts:
            continue
        rel = path.relative_to(root)
        files.append(path)
        parts.append(f"- {rel}")

    parts.append("")
    parts.append("## File contents")
    for path in files:
        rel = path.relative_to(root)
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            text = f"<read error: {exc}>"
        if max_file_chars is not None and len(text) > max_file_chars:
            text = text[:max_file_chars] + "\n...<truncated>"
        parts.append(f"\n### {rel}\n```\n{text}\n```")
    return "\n".join(parts)


def main() -> None:
    stored = persisted_rows_to_openai_messages(load_messages(SESSION_ID))
    current_context = build_current_style_context(stored)
    trimmed_context = build_trimmed_context(stored)
    tools = sample_tools()
    tool_prompt = tools_to_system_prompt(tools)
    read_codebase_prompt = f"baca codebase {TARGET_PATH}"
    full_codebase_snapshot = load_codebase_snapshot(TARGET_PATH)
    trimmed_codebase_snapshot = load_codebase_snapshot(TARGET_PATH, max_file_chars=900)
    full_tool_result_message = {
        "role": "assistant",
        "content": f"> **[Tool]** `read_codebase({TARGET_PATH})`\n\n> **[Result]** `read_codebase` →\n```\n{full_codebase_snapshot}\n```",
    }
    trimmed_tool_result_message = {
        "role": "assistant",
        "content": f"> **[Tool]** `read_codebase({TARGET_PATH})`\n\n> **[Result]** `read_codebase` →\n```\n{trimmed_codebase_snapshot}\n```",
    }

    baseline_payload = {
        "messages": current_context + [{"role": "user", "content": read_codebase_prompt}],
        "system_prompt": tool_prompt,
    }
    compact_payload = {
        "messages": trimmed_context + [{"role": "user", "content": read_codebase_prompt}],
        "system_prompt": tool_prompt,
    }
    pruned_tool_payload = {
        "messages": trimmed_context + [{"role": "user", "content": read_codebase_prompt}],
        "system_prompt": tools_to_system_prompt(sample_tools()[:2]),
    }
    post_tool_baseline_payload = {
        "messages": current_context + [{"role": "user", "content": read_codebase_prompt}, full_tool_result_message],
        "system_prompt": tool_prompt,
    }
    post_tool_compact_payload = {
        "messages": trimmed_context + [{"role": "user", "content": read_codebase_prompt}, trimmed_tool_result_message],
        "system_prompt": tools_to_system_prompt(sample_tools()[:2]),
    }

    report = {
        "session_id": SESSION_ID,
        "stored_message_count": len(stored),
        "assistant_message_count": sum(1 for m in stored if m.get("role") == "assistant"),
        "baseline_chars": chars(baseline_payload),
        "compact_history_chars": chars(compact_payload),
        "compact_plus_pruned_tools_chars": chars(pruned_tool_payload),
        "post_tool_baseline_chars": chars(post_tool_baseline_payload),
        "post_tool_compact_chars": chars(post_tool_compact_payload),
        "full_codebase_snapshot_chars": len(full_codebase_snapshot),
        "trimmed_codebase_snapshot_chars": len(trimmed_codebase_snapshot),
        "history_only_current_chars": chars(current_context),
        "history_only_trimmed_chars": chars(trimmed_context),
        "tool_prompt_chars": len(tool_prompt),
        "tool_prompt_pruned_chars": len(tools_to_system_prompt(sample_tools()[:2])),
        "largest_message_chars": max((len(m.get("content", "")) for m in stored), default=0),
    }

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
