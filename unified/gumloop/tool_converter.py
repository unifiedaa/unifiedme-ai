"""
Tool conversion utilities for Gumloop.
Converts Claude tool_use/tool_result to text format and parses tool calls from response.
"""
import json
import re
import uuid
from typing import List, Dict, Any, Optional, Tuple

def tools_to_system_prompt(tools: List[Dict[str, Any]]) -> str:
    """Convert tool definitions to system prompt format."""
    if not tools:
        return ""

    lines = ["You have access to the following tools:\n"]
    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "")
        schema = tool.get("input_schema", {})

        lines.append(f"<tool name=\"{name}\">")
        if desc:
            lines.append(f"<description>{desc}</description>")
        if schema:
            lines.append(f"<parameters>{json.dumps(schema, ensure_ascii=False)}</parameters>")
        lines.append("</tool>\n")

    lines.append("""
When you need to use a tool, output it in this exact format:
<tool_use>
<name>tool_name</name>
<input>{"param": "value"}</input>
</tool_use>

You can use multiple tools in one response. After outputting tool_use blocks, wait for the tool results before continuing.
""")
    return "\n".join(lines)

def convert_message_content(content: Any) -> Tuple[str, List[Dict[str, Any]], List[str]]:
    """
    Convert message content to text, extracting tool_use/tool_result blocks and image URLs.
    Returns (text_content, tool_blocks, image_urls)
    """
    if isinstance(content, str):
        return content, [], []

    if not isinstance(content, list):
        return str(content), [], []

    text_parts = []
    tool_blocks = []
    image_urls = []

    for block in content:
        if not isinstance(block, dict):
            continue

        btype = block.get("type", "")

        if btype == "text":
            text_parts.append(block.get("text", ""))

        elif btype == "tool_use":
            tool_blocks.append({
                "type": "tool_use",
                "id": block.get("id", f"toolu_{uuid.uuid4().hex[:24]}"),
                "name": block.get("name", ""),
                "input": block.get("input", {})
            })

        elif btype == "tool_result":
            tool_blocks.append({
                "type": "tool_result",
                "tool_use_id": block.get("tool_use_id", ""),
                "content": block.get("content", ""),
                "is_error": block.get("is_error", False)
            })

        elif btype == "image_url":
            img_url = block.get("image_url", {})
            url = img_url.get("url", "") if isinstance(img_url, dict) else str(img_url)
            if url:
                image_urls.append(url)

    return "\n".join(text_parts), tool_blocks, image_urls

_MAX_TOOL_RESULT_CHARS = 3000
_FILE_BOUNDARY = "═" * 60


def tool_result_to_text(tool_result: Dict[str, Any]) -> str:
    """Convert tool_result block to plain authoritative text for model history."""
    tool_use_id = tool_result.get("tool_use_id", "")
    content = tool_result.get("content", "")
    is_error = tool_result.get("is_error", False)
    tool_name = str(tool_result.get("tool_name", "") or "").strip()

    if isinstance(content, list):
        text_parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif isinstance(item, str):
                text_parts.append(item)
        content = "\n".join(text_parts)

    if len(content) > _MAX_TOOL_RESULT_CHARS:
        content = content[:_MAX_TOOL_RESULT_CHARS] + f"\n[truncated at {_MAX_TOOL_RESULT_CHARS} chars — use read with offset to see more]"

    status = "ERROR" if is_error else "OK"
    header = f"{_FILE_BOUNDARY}\n[TOOL RESULT] tool={tool_name or 'unknown'} id={tool_use_id} status={status}\n{_FILE_BOUNDARY}"
    footer = f"\n{_FILE_BOUNDARY} END tool={tool_name or 'unknown'} {_FILE_BOUNDARY}"
    return f"{header}\n{content}{footer}" if content else header

def tool_use_to_text(tool_use: Dict[str, Any]) -> str:
    """Convert tool_use block to text format (for assistant messages in history)."""
    name = tool_use.get("name", "")
    input_data = tool_use.get("input", {})
    tool_id = tool_use.get("id", "")

    input_json = json.dumps(input_data, ensure_ascii=False)
    return f"<tool_use id=\"{tool_id}\">\n<name>{name}</name>\n<input>{input_json}</input>\n</tool_use>"

def merge_consecutive_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result = []
    pending_role = None
    pending_contents = []
    pending_images: List[str] = []

    def _flush():
        if pending_role and pending_contents:
            entry = {"role": pending_role, "content": "\n\n".join(pending_contents)}
            if pending_images:
                entry["_images"] = list(pending_images)
            result.append(entry)

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if role == pending_role:
            if content:
                pending_contents.append(content)
            pending_images.extend(msg.get("_images", []))
        else:
            _flush()
            pending_role = role
            pending_contents = [content] if content else []
            pending_images = list(msg.get("_images", []))

    _flush()
    return result

def convert_messages_simple(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Convert messages without embedding tools (tools are set via REST API).
    Only converts tool_use/tool_result blocks to text format.
    Handles both Claude-native and OpenAI tool formats.
    """
    result = []
    seen_tool_result_ids = set()

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        text_content, tool_blocks, image_urls = convert_message_content(content)

        if role == "assistant":
            parts = []
            if text_content:
                parts.append(text_content)
            for block in tool_blocks:
                if block.get("type") == "tool_use":
                    parts.append(tool_use_to_text(block))
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                tc_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                try:
                    input_data = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    input_data = {"raw": func.get("arguments", "")}
                parts.append(tool_use_to_text({
                    "id": tc_id,
                    "name": func.get("name", ""),
                    "input": input_data,
                }))
            if parts:
                result.append({"role": "assistant", "content": "\n".join(parts)})

        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            if tool_call_id and tool_call_id in seen_tool_result_ids:
                continue
            if tool_call_id:
                seen_tool_result_ids.add(tool_call_id)
            tool_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False) if content else ""
            result.append({"role": "user", "content": tool_result_to_text({
                "tool_use_id": tool_call_id,
                "tool_name": msg.get("name", ""),
                "content": tool_content,
                "is_error": msg.get("is_error", False),
            })})

        elif role == "user":
            parts = []
            if text_content:
                parts.append(text_content)
            for block in tool_blocks:
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    if tool_use_id and tool_use_id in seen_tool_result_ids:
                        continue
                    if tool_use_id:
                        seen_tool_result_ids.add(tool_use_id)
                    parts.append(tool_result_to_text(block))
            if parts:
                msg_entry: Dict[str, Any] = {"role": "user", "content": "\n".join(parts)}
                if image_urls:
                    msg_entry["_images"] = image_urls
                result.append(msg_entry)

    return merge_consecutive_messages(result)

def convert_messages_with_tools(
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    system: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Convert messages with tool_use/tool_result to plain text format.
    Handles both Claude-native and OpenAI tool formats.
    Includes deduplication of tool_results to prevent infinite loops.
    """
    result = []
    seen_tool_result_ids = set()  # Track processed tool_result IDs

    # Add system prompt with tools
    system_parts = []
    if system:
        system_parts.append(system)
    if tools:
        system_parts.append(tools_to_system_prompt(tools))

    if system_parts:
        result.append({"role": "user", "content": f"[System]: {chr(10).join(system_parts)}"})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        text_content, tool_blocks, image_urls = convert_message_content(content)

        if role == "assistant":
            parts = []
            if text_content:
                parts.append(text_content)
            for block in tool_blocks:
                if block.get("type") == "tool_use":
                    parts.append(tool_use_to_text(block))
            for tc in msg.get("tool_calls", []):
                func = tc.get("function", {})
                tc_id = tc.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
                try:
                    input_data = json.loads(func.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    input_data = {"raw": func.get("arguments", "")}
                parts.append(tool_use_to_text({
                    "id": tc_id,
                    "name": func.get("name", ""),
                    "input": input_data,
                }))

            if parts:
                result.append({"role": "assistant", "content": "\n".join(parts)})

        elif role == "tool":
            tool_call_id = msg.get("tool_call_id", "")
            if tool_call_id and tool_call_id in seen_tool_result_ids:
                continue
            if tool_call_id:
                seen_tool_result_ids.add(tool_call_id)
            tool_content = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False) if content else ""
            result.append({"role": "user", "content": tool_result_to_text({
                "tool_use_id": tool_call_id,
                "tool_name": msg.get("name", ""),
                "content": tool_content,
                "is_error": msg.get("is_error", False),
            })})

        elif role == "user":
            parts = []
            if text_content:
                parts.append(text_content)
            for block in tool_blocks:
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    if tool_use_id and tool_use_id in seen_tool_result_ids:
                        continue
                    if tool_use_id:
                        seen_tool_result_ids.add(tool_use_id)
                    parts.append(tool_result_to_text(block))

            if parts:
                msg_entry: Dict[str, Any] = {"role": "user", "content": "\n".join(parts)}
                if image_urls:
                    msg_entry["_images"] = image_urls
                result.append(msg_entry)

    return merge_consecutive_messages(result)

# Regex patterns for parsing tool calls
TOOL_USE_BLOCK_PATTERN = re.compile(
    r"<tool_use\b([^>]*)>(.*?)</tool_use>",
    re.DOTALL | re.IGNORECASE,
)

TOOL_USE_ID_ATTR_PATTERN = re.compile(
    r'\bid\s*=\s*"([^"]*)"',
    re.IGNORECASE,
)

TOOL_USE_NAME_PATTERN = re.compile(
    r"<name\b[^>]*>(.*?)</name>",
    re.DOTALL | re.IGNORECASE,
)

TOOL_USE_INPUT_PATTERN = re.compile(
    r"<input\b[^>]*>(.*?)</input>",
    re.DOTALL | re.IGNORECASE,
)

def parse_tool_calls(text: str) -> Tuple[str, List[Dict[str, Any]]]:
    """
    Parse tool calls from model output.
    Returns (remaining_text, tool_use_blocks)
    """
    tool_uses = []

    for match in TOOL_USE_BLOCK_PATTERN.finditer(text):
        attrs = match.group(1) or ""
        block_body = match.group(2) or ""

        id_match = TOOL_USE_ID_ATTR_PATTERN.search(attrs)
        name_match = TOOL_USE_NAME_PATTERN.search(block_body)
        input_match = TOOL_USE_INPUT_PATTERN.search(block_body)

        if not name_match or not input_match:
            continue

        tool_id = (id_match.group(1).strip() if id_match else "") or f"toolu_{uuid.uuid4().hex[:24]}"
        name = name_match.group(1).strip()
        input_str = input_match.group(1).strip()

        try:
            input_data = json.loads(input_str)
        except json.JSONDecodeError:
            input_data = {"raw": input_str}

        tool_uses.append({
            "type": "tool_use",
            "id": tool_id,
            "name": name,
            "input": input_data
        })

    # Remove tool_use blocks from text
    remaining_text = TOOL_USE_BLOCK_PATTERN.sub("", text).strip()

    return remaining_text, tool_uses

def detect_tool_loop(messages: List[Dict[str, Any]], threshold: int = 3) -> Optional[str]:
    """
    Detect if the same tool is being called repeatedly (potential infinite loop).

    Checks for:
    1. Same tool called N times with same input consecutively
    2. Duplicate tool_result IDs across messages
    """
    recent_calls = []
    seen_tool_result_ids = set()
    duplicate_results = []

    for msg in messages[-15:]:  # Check last 15 messages
        content = msg.get("content", "")
        role = msg.get("role", "")

        _, tool_blocks, _ = convert_message_content(content)

        if role == "assistant":
            for block in tool_blocks:
                if block.get("type") == "tool_use":
                    call_sig = (block.get("name"), json.dumps(block.get("input", {}), sort_keys=True))
                    recent_calls.append(call_sig)

        elif role == "user":
            for block in tool_blocks:
                if block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id", "")
                    if tool_use_id:
                        if tool_use_id in seen_tool_result_ids:
                            duplicate_results.append(tool_use_id)
                        seen_tool_result_ids.add(tool_use_id)

    # Check for consecutive identical tool calls
    if len(recent_calls) >= threshold:
        last_call = recent_calls[-1]
        consecutive = sum(1 for c in recent_calls[-threshold:] if c == last_call)
        if consecutive >= threshold:
            return f"Detected infinite loop: tool '{last_call[0]}' called {consecutive} times consecutively with same input"

    # Check for duplicate tool_results (sign of message ordering issue)
    if len(duplicate_results) >= 2:
        return f"Detected duplicate tool_results: {duplicate_results[:3]}. This may cause infinite loops."

    return None


_TOOL_ARG_FIXES: Dict[str, Dict[str, str]] = {
    "read": {"path": "filePath", "file": "filePath", "file_path": "filePath"},
    "write": {"path": "filePath", "file": "filePath", "file_path": "filePath"},
    "edit": {"path": "filePath", "file": "filePath", "file_path": "filePath"},
    "glob": {"dir": "path", "directory": "path"},
    "grep": {"dir": "path", "directory": "path", "regex": "pattern"},
    "bash": {"cmd": "command", "shell": "command"},
    "lsp_diagnostics": {"path": "filePath", "file": "filePath"},
    "lsp_symbols": {"path": "filePath", "file": "filePath"},
}


def fix_tool_args(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize tool call arguments for known model mistakes."""
    fixes = _TOOL_ARG_FIXES.get(name)
    if not fixes or not isinstance(args, dict):
        return args
    for wrong_key, correct_key in fixes.items():
        if wrong_key in args and correct_key not in args:
            args[correct_key] = args.pop(wrong_key)
    return args
