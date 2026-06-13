"""Pydantic models for the Unified AI Proxy."""

from __future__ import annotations

import time
from typing import Any, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# OpenAI Chat Completion (request / response)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: Any = None
    name: Optional[str] = None
    tool_calls: Optional[list[Any]] = None
    tool_call_id: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatMessage]
    stream: bool = False
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stop: Optional[Any] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    tools: Optional[list[Any]] = None
    tool_choice: Optional[Any] = None
    response_format: Optional[Any] = None
    # Pass-through: allow arbitrary extra fields
    model_config = {"extra": "allow"}


class ChatCompletionChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: Optional[str] = "stop"


class UsageInfo(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    id: str = ""
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = ""
    choices: list[ChatCompletionChoice] = []
    usage: Optional[UsageInfo] = None


# ---------------------------------------------------------------------------
# Account management
# ---------------------------------------------------------------------------

class AccountInfo(BaseModel):
    id: int
    email: str
    status: str  # active, failed, trash, banned
    kiro_status: Optional[str] = None
    cb_status: Optional[str] = None
    kiro_credits: Optional[float] = None
    cb_credits: Optional[float] = None
    kiro_error: Optional[str] = None
    cb_error: Optional[str] = None
    kiro_error_count: int = 0
    cb_error_count: int = 0
    last_used_kiro: Optional[str] = None
    last_used_cb: Optional[str] = None
    created_at: Optional[str] = None


class AccountCreate(BaseModel):
    accounts: list[str]  # ["email:password", ...]
    providers: list[str] = ["kiro", "codebuddy", "chatbai"]


class BatchLoginRequest(BaseModel):
    accounts: list[str]  # ["email:password", ...]
    providers: list[str] = ["kiro", "codebuddy", "chatbai"]
    headless: bool = True
    concurrency: int = 1  # parallel browser instances
    mcp_urls: list[str] = []  # MCP server URLs to attach after Gumloop login
    gumloop_mode: str = "full"  # "full" (browser) or "relogin" (Firebase signInWithPassword)
    # Valid providers: kiro, codebuddy, wavespeed, gumloop


# ---------------------------------------------------------------------------
# API keys
# ---------------------------------------------------------------------------

class ApiKeyInfo(BaseModel):
    id: int
    key_prefix: str
    name: str
    active: bool
    created_at: Optional[str] = None
    last_used: Optional[str] = None
    usage_count: int = 0


class ApiKeyCreate(BaseModel):
    name: str = "default"


class ApiKeyFull(ApiKeyInfo):
    """Returned only on creation — includes the full key."""
    full_key: str


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

class StatsResponse(BaseModel):
    total_requests: int = 0
    requests_by_tier: dict[str, int] = {}
    requests_by_model: dict[str, int] = {}
    active_accounts_kiro: int = 0
    active_accounts_cb: int = 0
    total_accounts: int = 0
    api_keys_active: int = 0
