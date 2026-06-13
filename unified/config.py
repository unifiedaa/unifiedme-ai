"""Unified AI Proxy — configuration constants and model routing."""

import os
from enum import Enum
from pathlib import Path

# ---------------------------------------------------------------------------
# Network
# ---------------------------------------------------------------------------
LISTEN_HOST = os.getenv("UNIFIED_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("UNIFIED_PORT", "1430"))

# DEPRECATED: Kiro-Go binary no longer needed — API calls handled directly in Python.
# Kept for backward compat with account_manager credit refresh (will fail gracefully if Go not running).
KIRO_UPSTREAM = os.getenv("KIRO_UPSTREAM", "http://127.0.0.1:1434")
CODEBUDDY_UPSTREAM = os.getenv("CODEBUDDY_UPSTREAM", "https://www.codebuddy.ai")
WAVESPEED_UPSTREAM = os.getenv("WAVESPEED_UPSTREAM", "https://llm.wavespeed.ai")
CHATBAI_UPSTREAM = os.getenv("CHATBAI_UPSTREAM", "https://api.b.ai")

# Windsurf (sidecar Node.js proxy)
WINDSURF_SIDECAR_PORT = int(os.getenv("WINDSURF_PORT", "3003"))
WINDSURF_UPSTREAM = os.getenv("WINDSURF_UPSTREAM", f"http://127.0.0.1:{WINDSURF_SIDECAR_PORT}")
WINDSURF_INTERNAL_KEY = os.getenv("WINDSURF_INTERNAL_KEY", "wf-internal-secret")

# Gumloop
GUMLOOP_API_BASE = os.getenv("GUMLOOP_API_BASE", "https://api.gumloop.com")
GUMLOOP_WS_URL = os.getenv("GUMLOOP_WS_URL", "wss://ws.gumloop.com/ws/gummies")
GL_DEFAULT_CREDITS = 0.0  # No credit tracking — usage count only

# ---------------------------------------------------------------------------
# Auth / Admin
# ---------------------------------------------------------------------------
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "kUcingku0")
KIRO_ADMIN_PASSWORD = os.getenv("KIRO_ADMIN_PASSWORD", "kUcingku0")

# ---------------------------------------------------------------------------
# Credit defaults
# ---------------------------------------------------------------------------
KIRO_DEFAULT_CREDITS = 550.0
CB_DEFAULT_CREDITS = 250.0
SKBOSS_DEFAULT_CREDITS = 2.0  # $2 free signup credit
WS_DEFAULT_CREDITS = 1.0  # $1 trial credit
CBAI_DEFAULT_CREDITS = 1.0  # ChatBAI signup bonus credits (estimated $1 worth)

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
_VERSION_FILE = BASE_DIR.parent / "VERSION"
VERSION = _VERSION_FILE.read_text().strip() if _VERSION_FILE.exists() else "0.0.0"

# Central API for version check
CENTRAL_API_URL = os.getenv("CENTRAL_API_URL", "https://unified-api.roubot71.workers.dev")

# Database
# ---------------------------------------------------------------------------
DATA_DIR = BASE_DIR / "data"
DB_PATH = str(DATA_DIR / "unified.db")

# ---------------------------------------------------------------------------
# Login scripts
# ---------------------------------------------------------------------------
AUTH_DIR = BASE_DIR.parent          # auth/
AUTH_SCRIPT = AUTH_DIR / "login.py"
WAVESPEED_DIR = BASE_DIR.parent / "wavespeed"
WAVESPEED_SCRIPT = WAVESPEED_DIR / "register.py"
GUMLOOP_SCRIPT = AUTH_DIR / "backup_gumloop_cli" / "mcp_custom" / "intercept_gumloop_university.py"
GUMLOOP_RELOGIN_SCRIPT = AUTH_DIR / "gumloop_login.py"
CB_INTERCEPT_SCRIPT = AUTH_DIR / "cb_login_intercept.py"
CHATBAI_DIR = BASE_DIR.parent / "chatbai"
CHATBAI_SCRIPT = CHATBAI_DIR / "register.py"
# Windows uses Scripts/, Linux uses bin/
_VENV_BIN = "Scripts" if os.name == "nt" else "bin"
PYTHON_BIN = AUTH_DIR / ".venv" / _VENV_BIN / "python"

# ---------------------------------------------------------------------------
# Proxy (outbound)
# ---------------------------------------------------------------------------
PROXY_ENABLED = os.getenv("PROXY_ENABLED", "false").lower() in ("true", "1", "yes")
PROXY_URL = os.getenv("PROXY_URL", "")  # e.g. http://user:pass@host:port or socks5://host:port

# ---------------------------------------------------------------------------
# Tier enum
# ---------------------------------------------------------------------------

class Tier(str, Enum):
    STANDARD = "standard"       # Kiro
    MAX = "max"                 # CodeBuddy
    WAVESPEED = "wavespeed"     # WaveSpeed LLM
    MAX_GL = "max_gl"           # Gumloop
    CHATBAI = "chatbai"         # ChatBAI (chat.b.ai)
    SKILLBOSS = "skillboss"     # SkillBoss
    WINDSURF = "windsurf"       # Windsurf IDE (sidecar)
    THEROUTER = "therouter"     # TheRouter LLM Gateway


# ---------------------------------------------------------------------------
# Model → Tier routing table
# ---------------------------------------------------------------------------
_STANDARD_MODELS = [
    "auto",
    "claude-sonnet-4.5",
    "claude-sonnet-4",
    "claude-haiku-4.5",
    "deepseek-3.2",
    "minimax-m2.5",
    "minimax-m2.1",
    "glm-5",
    "qwen3-coder-next",
]

_MAX_MODELS = [
    "gemini-3.1-pro",
    "gemini-3.1-flash-lite",
    "gemini-3.0-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.2",
    "gpt-5.3-codex",
    "gpt-5.2-codex",
    "gpt-5.1",
    "gpt-5.1-codex",
    "gpt-5.1-codex-max",
    "gpt-5.1-codex-mini",
    "o3-2025-04-16",
    "o4-mini-2025-04-16",
    "deepseek-v3-2-volc",
    "claude-opus-4.6",
    "kimi-k2.5",
]

_MAX_GL_MODELS = [
    "gl-claude-opus-4-7",
    "gl-claude-opus-4-6",
    "gl-claude-sonnet-4-6",
    "gl-claude-sonnet-4-5",
    "gl-claude-haiku-4-5",
    "gl-gpt-5.4",
    "gl-gpt-5.4-mini",
    "gl-gpt-5.4-nano",
    "gl-gpt-5.3-code",
    "gl-gpt-5.2",
    "gl-gpt-5.2-codex",
    "gl-gpt-5.5",
    "gl-gemini-3.1-pro-preview",
    "gl-kimi-k2.6",
]

_MAX_GL2_MODELS = [m.replace("gl-", "gl2-", 1) for m in _MAX_GL_MODELS]

# Dot-format aliases for Claude models (GPT already uses dots)
_MAX_GL_DOT_ALIASES = {
    "gl-claude-opus-4.7": "gl-claude-opus-4-7",
    "gl-claude-opus-4.6": "gl-claude-opus-4-6",
    "gl-claude-sonnet-4.6": "gl-claude-sonnet-4-6",
    "gl-claude-sonnet-4.5": "gl-claude-sonnet-4-5",
    "gl-claude-haiku-4.5": "gl-claude-haiku-4-5",
}

_MAX_GL2_DOT_ALIASES = {k.replace("gl-", "gl2-", 1): v.replace("gl-", "gl2-", 1) for k, v in _MAX_GL_DOT_ALIASES.items()}

# WaveSpeed models use provider/model format — any model with "/" is WaveSpeed
# We also define popular aliases without the provider prefix
_WAVESPEED_ALIASES = [
    "new-claude-opus-4.7",
    "new-claude-opus-4.6",
    "new-claude-sonnet-4.5",
    "new-claude-sonnet-4",
    "new-claude-haiku-4.5",
    "new-gpt-5.2",
    "new-gpt-4o",
    "new-gemini-3-pro",
    "new-gemini-2.5-flash",
    "new-grok-4",
    "new-deepseek-r1",
    "new-llama-4-maverick",
    "new-qwen-max",
]

# ChatBAI models (bchatai- prefix)
_CHATBAI_MODELS = [
    # Free
    "bchatai-claude-haiku-4.5",
    "bchatai-gpt-5.4-nano",
    "bchatai-gpt-5.4-mini",
    "bchatai-gpt-5-mini",
    "bchatai-gpt-5-nano",
    "bchatai-gpt-5.2",
    "bchatai-glm-5",
    "bchatai-kimi-k2.5",
    "bchatai-minimax-m2.5",
    "bchatai-deepseek-v4-flash",
    "bchatai-deepseek-v4-pro",
    # Premium (requires topup)
    "bchatai-gpt-5.5",
    "bchatai-gpt-5.4",
    "bchatai-gpt-5.4-pro",
    "bchatai-claude-opus-4.7",
    "bchatai-claude-opus-4.6",
    "bchatai-claude-opus-4.5",
    "bchatai-claude-sonnet-4.6",
    "bchatai-claude-sonnet-4.5",
]

# SkillBoss models (skboss- prefix)
_SKILLBOSS_MODELS = [
    "skboss-claude-opus-4.7",
    "skboss-claude-opus-4.6",
    "skboss-claude-sonnet-4.6",
    "skboss-claude-haiku-4.5",
    "skboss-gpt-5.4",
    "skboss-gpt-5.2",
    "skboss-gpt-5.1",
    "skboss-gpt-5.4-mini",
    "skboss-gpt-5-nano",
    "skboss-gemini-2.5-flash",
    "skboss-gemini-3.1-pro",
]

# Windsurf models (windsurf- prefix)
# Full catalog from WindsurfAPI sidecar — 140+ models
_WINDSURF_MODELS = [
    # Claude (Anthropic)
    "windsurf-claude-3.5-sonnet",
    "windsurf-claude-3.7-sonnet",
    "windsurf-claude-3.7-sonnet-thinking",
    "windsurf-claude-4-sonnet",
    "windsurf-claude-4-sonnet-thinking",
    "windsurf-claude-4-opus",
    "windsurf-claude-4-opus-thinking",
    "windsurf-claude-4.1-opus",
    "windsurf-claude-4.1-opus-thinking",
    "windsurf-claude-4.5-haiku",
    "windsurf-claude-4.5-sonnet",
    "windsurf-claude-4.5-sonnet-thinking",
    "windsurf-claude-4.5-opus",
    "windsurf-claude-4.5-opus-thinking",
    "windsurf-claude-sonnet-4.6",
    "windsurf-claude-sonnet-4.6-thinking",
    "windsurf-claude-sonnet-4.6-1m",
    "windsurf-claude-sonnet-4.6-thinking-1m",
    "windsurf-claude-opus-4.6",
    "windsurf-claude-opus-4.6-thinking",
    "windsurf-claude-opus-4-7-low",
    "windsurf-claude-opus-4-7-medium",
    "windsurf-claude-opus-4-7-high",
    "windsurf-claude-opus-4-7-xhigh",
    "windsurf-claude-opus-4-7-medium-thinking",
    "windsurf-claude-opus-4-7-high-thinking",
    "windsurf-claude-opus-4-7-xhigh-thinking",
    "windsurf-claude-opus-4-7-max",
    # GPT (OpenAI)
    "windsurf-gpt-4o",
    "windsurf-gpt-4o-mini",
    "windsurf-gpt-4.1",
    "windsurf-gpt-4.1-mini",
    "windsurf-gpt-4.1-nano",
    "windsurf-gpt-5",
    "windsurf-gpt-5-medium",
    "windsurf-gpt-5-high",
    "windsurf-gpt-5-mini",
    "windsurf-gpt-5-codex",
    "windsurf-gpt-5.1",
    "windsurf-gpt-5.1-low",
    "windsurf-gpt-5.1-medium",
    "windsurf-gpt-5.1-high",
    "windsurf-gpt-5.1-fast",
    "windsurf-gpt-5.1-codex-low",
    "windsurf-gpt-5.1-codex-medium",
    "windsurf-gpt-5.1-codex-mini",
    "windsurf-gpt-5.1-codex-max-low",
    "windsurf-gpt-5.1-codex-max-medium",
    "windsurf-gpt-5.1-codex-max-high",
    "windsurf-gpt-5.2",
    "windsurf-gpt-5.2-low",
    "windsurf-gpt-5.2-high",
    "windsurf-gpt-5.2-xhigh",
    "windsurf-gpt-5.2-codex-low",
    "windsurf-gpt-5.2-codex-medium",
    "windsurf-gpt-5.2-codex-high",
    "windsurf-gpt-5.2-codex-xhigh",
    "windsurf-gpt-5.3-codex",
    "windsurf-gpt-5.3-codex-low",
    "windsurf-gpt-5.3-codex-high",
    "windsurf-gpt-5.3-codex-xhigh",
    "windsurf-gpt-5.4-low",
    "windsurf-gpt-5.4-medium",
    "windsurf-gpt-5.4-high",
    "windsurf-gpt-5.4-xhigh",
    "windsurf-gpt-5.4-mini-low",
    "windsurf-gpt-5.4-mini-medium",
    "windsurf-gpt-5.4-mini-high",
    "windsurf-gpt-5.4-mini-xhigh",
    "windsurf-gpt-5.5",
    "windsurf-gpt-5.5-low",
    "windsurf-gpt-5.5-medium",
    "windsurf-gpt-5.5-high",
    "windsurf-gpt-5.5-xhigh",
    # o-series (OpenAI)
    "windsurf-o3-mini",
    "windsurf-o3",
    "windsurf-o3-high",
    "windsurf-o3-pro",
    "windsurf-o4-mini",
    # Gemini (Google)
    "windsurf-gemini-2.5-pro",
    "windsurf-gemini-2.5-flash",
    "windsurf-gemini-3.0-pro",
    "windsurf-gemini-3.0-flash",
    "windsurf-gemini-3.0-flash-low",
    "windsurf-gemini-3.0-flash-high",
    "windsurf-gemini-3.1-pro-low",
    "windsurf-gemini-3.1-pro-high",
    # Grok (xAI)
    "windsurf-grok-3",
    "windsurf-grok-3-mini",
    "windsurf-grok-3-mini-thinking",
    "windsurf-grok-code-fast-1",
    # DeepSeek
    "windsurf-deepseek-v3",
    "windsurf-deepseek-v3-2",
    "windsurf-deepseek-v4",
    "windsurf-deepseek-r1",
    # Qwen
    "windsurf-qwen-3",
    # Kimi
    "windsurf-kimi-k2",
    "windsurf-kimi-k2-thinking",
    "windsurf-kimi-k2.5",
    "windsurf-kimi-k2-6",
    # GLM
    "windsurf-glm-4.7",
    "windsurf-glm-4.7-fast",
    "windsurf-glm-5",
    "windsurf-glm-5.1",
    # MiniMax
    "windsurf-minimax-m2.5",
    # Windsurf native
    "windsurf-swe-1.5",
    "windsurf-swe-1.5-fast",
    "windsurf-swe-1.5-thinking",
    "windsurf-swe-1.6",
    "windsurf-swe-1.6-fast",
    "windsurf-adaptive",
    "windsurf-arena-fast",
    "windsurf-arena-smart",
]

# TheRouter models (tr- prefix)
# Base models — each can have variants: :online, :thinking, :extended, :exacto
_THEROUTER_BASE_MODELS = [
    "claude-opus-4.7",
    "claude-opus-4.6",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.2",
]

_THEROUTER_VARIANTS = ["", ":online", ":thinking", ":extended", ":exacto"]

_THEROUTER_MODELS: list[str] = []
for _base in _THEROUTER_BASE_MODELS:
    for _var in _THEROUTER_VARIANTS:
        _THEROUTER_MODELS.append(f"tr-{_base}{_var}")

# TheRouter upstream
THEROUTER_UPSTREAM = os.getenv("THEROUTER_UPSTREAM", "https://api.therouter.ai")
TR_DEFAULT_CREDITS = 0.0  # Free tier — usage count only

# Build lookup: model_name → Tier
MODEL_TIER: dict[str, Tier] = {}

for m in _STANDARD_MODELS:
    MODEL_TIER[m] = Tier.STANDARD
    MODEL_TIER[f"{m}-thinking"] = Tier.STANDARD

for m in _MAX_MODELS:
    MODEL_TIER[m] = Tier.MAX

for m in _WAVESPEED_ALIASES:
    MODEL_TIER[m] = Tier.WAVESPEED

for m in _MAX_GL_MODELS:
    MODEL_TIER[m] = Tier.MAX_GL
for alias in _MAX_GL_DOT_ALIASES:
    MODEL_TIER[alias] = Tier.MAX_GL
for m in _MAX_GL2_MODELS:
    MODEL_TIER[m] = Tier.MAX_GL
for alias in _MAX_GL2_DOT_ALIASES:
    MODEL_TIER[alias] = Tier.MAX_GL

for m in _CHATBAI_MODELS:
    MODEL_TIER[m] = Tier.CHATBAI

for m in _SKILLBOSS_MODELS:
    MODEL_TIER[m] = Tier.SKILLBOSS

for m in _WINDSURF_MODELS:
    MODEL_TIER[m] = Tier.WINDSURF

for m in _THEROUTER_MODELS:
    MODEL_TIER[m] = Tier.THEROUTER

# Hidden from display lists (routing-only aliases + thinking variants)
_HIDDEN_ALIASES: set[str] = set(_MAX_GL_DOT_ALIASES.keys()) | set(_MAX_GL2_DOT_ALIASES.keys())
# Hide -thinking variants from model list (they still work for routing)
for _m in list(_STANDARD_MODELS):
    _thinking = f"{_m}-thinking"
    if _thinking in MODEL_TIER:
        _HIDDEN_ALIASES.add(_thinking)

# Flat lists for /v1/models
STANDARD_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.STANDARD]
MAX_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.MAX]
WAVESPEED_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.WAVESPEED]
MAX_GL_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.MAX_GL and k not in _HIDDEN_ALIASES]
CHATBAI_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.CHATBAI]
SKILLBOSS_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.SKILLBOSS]
WINDSURF_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.WINDSURF]
THEROUTER_MODELS: list[str] = [k for k, v in MODEL_TIER.items() if v == Tier.THEROUTER]
ALL_MODELS: list[str] = [k for k in MODEL_TIER if k not in _HIDDEN_ALIASES]


def get_tier(model: str) -> Tier | None:
    """Return the tier for a model name, or None if unknown.

    WaveSpeed models can be:
    - Explicit alias: new-claude-opus-4.7
    - Direct provider format: anthropic/claude-opus-4.7 (any model with /)
    - Prefix: new-<anything>
    """
    # Check explicit mapping first
    tier = MODEL_TIER.get(model)
    if tier is not None:
        return tier
    # Any model with "gl-" or "gl2-" prefix → MAX_GL (Gumloop)
    if model.startswith("gl-"):
        return Tier.MAX_GL
    if model.startswith("gl2-"):
        return Tier.MAX_GL
    # Any model with "new-" prefix → WaveSpeed
    if model.startswith("new-"):
        return Tier.WAVESPEED
    # Any model with "bchatai-" prefix → ChatBAI
    if model.startswith("bchatai-"):
        return Tier.CHATBAI
    # Any model with "skboss-" prefix → SkillBoss
    if model.startswith("skboss-"):
        return Tier.SKILLBOSS
    # Any model with "windsurf-" prefix → Windsurf
    if model.startswith("windsurf-"):
        return Tier.WINDSURF
    # Any model with "tr-" prefix → TheRouter
    if model.startswith("tr-"):
        return Tier.THEROUTER
    # Any model with "/" → WaveSpeed (provider/model format)
    if "/" in model:
        return Tier.WAVESPEED
    return None
