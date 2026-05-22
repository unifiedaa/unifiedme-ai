"""Proxy-native tool loop for GL2 models.

Intercepts tool_use from Gumloop, executes tools locally, feeds results back,
and only streams the final answer to OpenCode. OpenCode never sees tool chatter.
"""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncIterator

from .gumloop.client import send_chat, update_gummie_config
from .gumloop.parser import build_openai_chunk, build_openai_done
from .gumloop.tool_converter import (
    convert_messages_with_tools,
    fix_tool_args,
    parse_tool_calls,
    tools_to_system_prompt,
)
from .loop_tools import SUPPORTED_TOOLS, execute_tool

log = logging.getLogger("unified.proxy_tool_loop")

MAX_ITERATIONS = 15
MAX_INPUT_TOKENS = 150_000
MAX_OUTPUT_TOKENS = 32_000
MAX_CONSECUTIVE_FAILURES = 3
KEEPALIVE_INTERVAL = 5.0


@dataclass
class LoopBudget:
    max_input: int = MAX_INPUT_TOKENS
    max_output: int = MAX_OUTPUT_TOKENS
    max_iterations: int = MAX_ITERATIONS
    cumulative_input: int = 0
    cumulative_output: int = 0
    iteration: int = 0

    def record(self, usage: dict[str, Any]) -> None:
        self.cumulative_input += usage.get("prompt_tokens", 0) or usage.get("input_tokens", 0)
        self.cumulative_output += usage.get("completion_tokens", 0) or usage.get("output_tokens", 0)
        self.iteration += 1

    @property
    def exceeded(self) -> bool:
        return (
            self.cumulative_input >= self.max_input
            or self.cumulative_output >= self.max_output
            or self.iteration >= self.max_iterations
        )

    @property
    def summary(self) -> str:
        return (
            f"iterations={self.iteration}/{self.max_iterations} "
            f"input={self.cumulative_input}/{self.max_input} "
            f"output={self.cumulative_output}/{self.max_output}"
        )


def _format_tool_result_xml(tool_id: str, name: str, result: str) -> str:
    return f'<tool_result tool_use_id="{tool_id}" status="success">\n{result}\n</tool_result>'


def _format_tool_error_xml(tool_id: str, name: str, error: str) -> str:
    return f'<tool_result tool_use_id="{tool_id}" status="error">\n{error}\n</tool_result>'


async def _call_gumloop_collect(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: Any,
    turnstile: Any,
    interaction_id: str,
    proxy_url: str | None,
) -> tuple[str, dict[str, Any]]:
    """Call Gumloop and collect full response text + usage. Non-streaming."""
    full_text = ""
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    async for event in send_chat(
        gummie_id=gummie_id,
        messages=messages,
        auth=auth,
        turnstile=turnstile,
        interaction_id=interaction_id,
        proxy_url=proxy_url,
    ):
        etype = event.get("type", "")
        if etype == "text-delta":
            full_text += event.get("delta", "")
        elif etype == "finish":
            event_usage = event.get("usage") or {}
            usage["prompt_tokens"] += event_usage.get("input_tokens", 0)
            usage["completion_tokens"] += event_usage.get("output_tokens", 0)
            usage["total_tokens"] += event_usage.get("total_tokens", 0)
            if event.get("final", True):
                break
        elif etype == "error":
            raise RuntimeError(event.get("error", "Unknown Gumloop error"))

    return full_text, usage


def _extract_thinking(text: str) -> tuple[str, str | None]:
    """Extract thinking blocks and return (clean_text, reasoning_text)."""
    thinking_parts = re.findall(r"<thinking>\n?(.*?)</thinking>", text, flags=re.DOTALL)
    reasoning = "\n".join(p.strip() for p in thinking_parts if p.strip()) or None
    clean = re.sub(r"<thinking>\n?.*?</thinking>\n?", "", text, flags=re.DOTALL)
    return clean, reasoning


async def tool_loop_stream(
    gummie_id: str,
    messages: list[dict[str, Any]],
    auth: Any,
    turnstile: Any,
    display_model: str,
    interaction_id: str,
    proxy_url: str | None,
    gumloop_tools: list[dict[str, Any]],
    system_prompt: str,
    rehydration_info: dict[str, Any] | None = None,
) -> AsyncIterator[bytes]:
    """Run proxy-native tool loop. Yields SSE chunks for OpenCode."""

    stream_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    budget = LoopBudget()
    consecutive_failures = 0
    internal_messages: list[dict[str, Any]] = []
    total_tool_calls = 0

    yield build_openai_chunk(stream_id, display_model, role="assistant", created=created).encode()

    while not budget.exceeded:
        all_messages = messages + internal_messages

        try:
            raw_response, usage = await _call_gumloop_collect(
                gummie_id=gummie_id,
                messages=all_messages,
                auth=auth,
                turnstile=turnstile,
                interaction_id=interaction_id,
                proxy_url=proxy_url,
            )
        except Exception as e:
            log.error("[tool_loop] Gumloop call failed: %s", e)
            yield build_openai_chunk(
                stream_id, display_model,
                content=f"\n\nError communicating with model: {e}",
                created=created,
            ).encode()
            break

        budget.record(usage)

        clean_text, reasoning = _extract_thinking(raw_response)
        remaining_text, tool_uses = parse_tool_calls(clean_text)

        if not tool_uses:
            if reasoning:
                yield build_openai_chunk(
                    stream_id, display_model,
                    reasoning_content=reasoning,
                    created=created,
                ).encode()
            if remaining_text:
                yield build_openai_chunk(
                    stream_id, display_model,
                    content=remaining_text,
                    created=created,
                ).encode()
            break

        iteration_msg = f"[Executing {len(tool_uses)} tool(s) internally, iteration {budget.iteration + 1}...]"
        yield build_openai_chunk(
            stream_id, display_model,
            reasoning_content=iteration_msg,
            created=created,
        ).encode()

        internal_messages.append({"role": "assistant", "content": raw_response})

        tool_results: list[str] = []
        for tu in tool_uses:
            tool_name = tu.get("name", "")
            tool_id = tu.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
            tool_args = fix_tool_args(tool_name, tu.get("input", {}))
            total_tool_calls += 1

            if tool_name not in SUPPORTED_TOOLS:
                result_xml = _format_tool_error_xml(
                    tool_id, tool_name, f"Unknown tool: {tool_name}"
                )
                tool_results.append(result_xml)
                consecutive_failures += 1
                continue

            log.info("[tool_loop] iter=%d executing %s(%s)",
                     budget.iteration, tool_name, json.dumps(tool_args, ensure_ascii=False)[:200])

            result = await execute_tool(tool_name, tool_args)

            if result.startswith("ERROR:"):
                tool_results.append(_format_tool_error_xml(tool_id, tool_name, result))
                consecutive_failures += 1
            else:
                tool_results.append(_format_tool_result_xml(tool_id, tool_name, result))
                consecutive_failures = 0

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                log.warning("[tool_loop] %d consecutive failures, aborting", consecutive_failures)
                break

        internal_messages.append({"role": "user", "content": "\n".join(tool_results)})

        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            internal_messages.append({
                "role": "user",
                "content": "SYSTEM: Multiple tool failures. Provide your best answer with available information. Do not use more tools.",
            })
            try:
                final_text, final_usage = await _call_gumloop_collect(
                    gummie_id=gummie_id,
                    messages=messages + internal_messages,
                    auth=auth,
                    turnstile=turnstile,
                    interaction_id=interaction_id,
                    proxy_url=proxy_url,
                )
                budget.record(final_usage)
                clean_final, reasoning_final = _extract_thinking(final_text)
                if reasoning_final:
                    yield build_openai_chunk(stream_id, display_model, reasoning_content=reasoning_final, created=created).encode()
                if clean_final:
                    yield build_openai_chunk(stream_id, display_model, content=clean_final, created=created).encode()
            except Exception as e:
                yield build_openai_chunk(stream_id, display_model, content=f"\nFailed to get final answer: {e}", created=created).encode()
            break

    else:
        log.warning("[tool_loop] Budget exceeded: %s", budget.summary)
        internal_messages.append({
            "role": "user",
            "content": "SYSTEM: Token budget exceeded. Provide your best answer now with available information. Do not use any more tools.",
        })
        try:
            final_text, final_usage = await _call_gumloop_collect(
                gummie_id=gummie_id,
                messages=messages + internal_messages,
                auth=auth,
                turnstile=turnstile,
                interaction_id=interaction_id,
                proxy_url=proxy_url,
            )
            budget.record(final_usage)
            clean_final, reasoning_final = _extract_thinking(final_text)
            if reasoning_final:
                yield build_openai_chunk(stream_id, display_model, reasoning_content=reasoning_final, created=created).encode()
            if clean_final:
                yield build_openai_chunk(stream_id, display_model, content=clean_final, created=created).encode()
        except Exception as e:
            yield build_openai_chunk(stream_id, display_model, content=f"\nBudget exceeded, failed final call: {e}", created=created).encode()

    if rehydration_info and rehydration_info.get("injected"):
        mode = rehydration_info.get("mode", "delta")
        count = rehydration_info.get("count", 0)
        status_msg = f"\n\n_[Context synced: {count} messages injected ({mode})]_"
        yield build_openai_chunk(stream_id, display_model, content=status_msg, created=created).encode()

    if total_tool_calls > 0:
        loop_summary = f"\n\n_[Proxy tool loop: {total_tool_calls} tool calls across {budget.iteration} iterations, {budget.cumulative_input + budget.cumulative_output} internal tokens]_"
        yield build_openai_chunk(stream_id, display_model, content=loop_summary, created=created).encode()

    total_usage = {
        "prompt_tokens": budget.cumulative_input,
        "completion_tokens": budget.cumulative_output,
        "total_tokens": budget.cumulative_input + budget.cumulative_output,
    }
    yield build_openai_chunk(stream_id, display_model, finish_reason="stop", created=created, usage=total_usage).encode()
    yield build_openai_done().encode()
