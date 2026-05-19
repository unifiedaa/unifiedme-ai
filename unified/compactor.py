"""Local text compactor for Gumloop session history.

Produces a deterministic summary from DB messages without external LLM calls.
Strategy: extract key facts (topics, decisions, file paths, tool actions)
and compress into a bounded-length context block.
"""

from __future__ import annotations

import re
from typing import Any

_MAX_SUMMARY_CHARS = 4000
_MAX_MSG_PREVIEW = 300
_RECENT_WINDOW = 10


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts)
    return str(content or "")


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _extract_key_signals(text: str) -> list[str]:
    signals: list[str] = []
    file_paths = re.findall(r'[\w./\\-]+\.\w{1,10}', text)
    for fp in file_paths[:5]:
        if len(fp) > 4:
            signals.append(fp)
    decisions = re.findall(r'(?:decided|chose|will use|switched to|implemented|created|fixed|added|removed|updated)\s+[^.!?\n]{5,80}', text, re.IGNORECASE)
    for d in decisions[:3]:
        signals.append(d.strip())
    return signals


def compact_messages(messages: list[dict[str, Any]], max_chars: int = _MAX_SUMMARY_CHARS) -> str:
    """Compact a list of chat messages into a bounded summary string.

    Args:
        messages: list of dicts with 'role' and 'content' keys
        max_chars: maximum output length

    Returns:
        A compact text summary suitable for injection as context.
    """
    if not messages:
        return ""

    total = len(messages)
    topics: list[str] = []
    user_queries: list[str] = []
    assistant_actions: list[str] = []
    key_signals: list[str] = []

    for msg in messages:
        role = msg.get("role", "")
        text = _extract_content_text(msg.get("content", ""))
        if not text.strip():
            continue

        signals = _extract_key_signals(text)
        key_signals.extend(signals)

        if role == "user":
            preview = _truncate(text.strip().split("\n")[0], _MAX_MSG_PREVIEW)
            user_queries.append(preview)
        elif role == "assistant":
            first_line = text.strip().split("\n")[0]
            preview = _truncate(first_line, _MAX_MSG_PREVIEW)
            assistant_actions.append(preview)

    unique_signals = list(dict.fromkeys(key_signals))[:15]

    lines: list[str] = []
    lines.append(f"[Session summary: {total} messages compacted]")

    if unique_signals:
        lines.append(f"Key artifacts: {', '.join(unique_signals[:10])}")

    if user_queries:
        lines.append(f"User topics ({len(user_queries)} queries):")
        budget = max_chars // 3
        used = 0
        for q in user_queries:
            if used + len(q) + 4 > budget:
                lines.append(f"  ... and {len(user_queries) - user_queries.index(q)} more")
                break
            lines.append(f"  - {q}")
            used += len(q) + 4

    if assistant_actions:
        lines.append(f"Assistant actions ({len(assistant_actions)} responses):")
        budget = max_chars // 3
        used = 0
        for a in assistant_actions:
            if used + len(a) + 4 > budget:
                lines.append(f"  ... and {len(assistant_actions) - assistant_actions.index(a)} more")
                break
            lines.append(f"  - {a}")
            used += len(a) + 4

    result = "\n".join(lines)
    if len(result) > max_chars:
        result = result[:max_chars - 3] + "..."
    return result


def should_compact(message_count: int, existing_summary_watermark: int, latest_message_id: int) -> bool:
    """Decide whether to regenerate the summary.

    Triggers when there are 10+ messages beyond the current summary watermark.
    """
    unsummarized = latest_message_id - existing_summary_watermark
    return message_count >= _RECENT_WINDOW and unsummarized >= 10
