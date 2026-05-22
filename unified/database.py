"""SQLite database layer (async via aiosqlite)."""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import time
import json
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH, DATA_DIR

log = logging.getLogger("unified.database")

_db: Optional[aiosqlite.Connection] = None


# ---------------------------------------------------------------------------
# Connection management
# ---------------------------------------------------------------------------

async def get_db() -> aiosqlite.Connection:
    global _db
    if _db is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _db = await aiosqlite.connect(DB_PATH)
        _db.row_factory = aiosqlite.Row
        await _db.execute("PRAGMA journal_mode=WAL")
        await _db.execute("PRAGMA foreign_keys=OFF")
    return _db


async def close_db() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    email           TEXT NOT NULL,
    password        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',  -- active, failed, trash, banned

    -- Kiro fields
    kiro_status         TEXT DEFAULT 'pending',  -- pending, ok, failed, exhausted
    kiro_access_token   TEXT DEFAULT '',
    kiro_refresh_token  TEXT DEFAULT '',
    kiro_profile_arn    TEXT DEFAULT '',
    kiro_credits        REAL DEFAULT 0,
    kiro_error          TEXT DEFAULT '',
    kiro_error_count    INTEGER DEFAULT 0,
    last_used_kiro      TEXT DEFAULT '',

    kiro_credits_total  REAL DEFAULT 0,
    kiro_credits_used   REAL DEFAULT 0,
    kiro_expires_at     TEXT DEFAULT '',

    -- CodeBuddy fields
    cb_status           TEXT DEFAULT 'pending',  -- pending, ok, failed, exhausted
    cb_api_key          TEXT DEFAULT '',
    cb_credits          REAL DEFAULT 0,
    cb_error            TEXT DEFAULT '',
    cb_error_count      INTEGER DEFAULT 0,
    last_used_cb        TEXT DEFAULT '',
    cb_expires_at       TEXT DEFAULT '',

    -- WaveSpeed fields
    ws_status           TEXT DEFAULT 'none',  -- none, pending, ok, failed, exhausted
    ws_api_key          TEXT DEFAULT '',
    ws_credits          REAL DEFAULT 0,       -- $1 default, deduct usage.cost
    ws_error            TEXT DEFAULT '',
    ws_error_count      INTEGER DEFAULT 0,
    last_used_ws        TEXT DEFAULT '',

    -- Gumloop fields
    gl_status           TEXT DEFAULT 'none',  -- none, pending, ok, failed, exhausted, banned
    gl_refresh_token    TEXT DEFAULT '',
    gl_user_id          TEXT DEFAULT '',
    gl_gummie_id        TEXT DEFAULT '',
    gl_id_token         TEXT DEFAULT '',
    gl_credits          REAL DEFAULT 0,
    gl_error            TEXT DEFAULT '',
    gl_error_count      INTEGER DEFAULT 0,
    gl_exhausted_until  TEXT DEFAULT '',  -- ISO timestamp: temporary exhaustion cooldown
    last_used_gl        TEXT DEFAULT '',

    -- ChatBAI fields
    cbai_status         TEXT DEFAULT 'none',  -- none, pending, ok, failed, exhausted, banned
    cbai_api_key        TEXT DEFAULT '',
    cbai_session_token  TEXT DEFAULT '',
    cbai_credits        REAL DEFAULT 0,
    cbai_error          TEXT DEFAULT '',
    cbai_error_count    INTEGER DEFAULT 0,
    last_used_cbai      TEXT DEFAULT '',

    -- SkillBoss fields
    skboss_status       TEXT DEFAULT 'none',  -- none, pending, ok, failed, exhausted, banned
    skboss_api_key      TEXT DEFAULT '',
    skboss_credits      REAL DEFAULT 0,
    skboss_error        TEXT DEFAULT '',
    skboss_error_count  INTEGER DEFAULT 0,
    last_used_skboss    TEXT DEFAULT '',

    -- Windsurf fields
    windsurf_status       TEXT DEFAULT 'none',  -- none, pending, ok, failed, exhausted, banned
    windsurf_api_key      TEXT DEFAULT '',
    windsurf_credits      REAL DEFAULT 0,
    windsurf_error        TEXT DEFAULT '',
    windsurf_error_count  INTEGER DEFAULT 0,
    last_used_windsurf    TEXT DEFAULT '',

    created_at      TEXT DEFAULT (datetime('now')),
    updated_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS api_keys (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key_hash    TEXT NOT NULL UNIQUE,
    key_prefix  TEXT NOT NULL,
    name        TEXT NOT NULL DEFAULT 'default',
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT DEFAULT (datetime('now')),
    last_used   TEXT DEFAULT '',
    usage_count INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS opencode_session_registry (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id       INTEGER NOT NULL UNIQUE,
    chat_session_id  INTEGER NOT NULL,
    last_model       TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now')),
    updated_at       TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS usage_logs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    api_key_id       INTEGER,
    account_id       INTEGER,
    model            TEXT NOT NULL,
    tier             TEXT NOT NULL,
    status_code      INTEGER DEFAULT 200,
    latency_ms       INTEGER DEFAULT 0,
    request_headers  TEXT DEFAULT '',
    request_body     TEXT DEFAULT '',
    response_headers TEXT DEFAULT '',
    response_body    TEXT DEFAULT '',
    error_message    TEXT DEFAULT '',
    created_at       TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_accounts_status ON accounts(status);
CREATE INDEX IF NOT EXISTS idx_accounts_kiro_status ON accounts(kiro_status);
CREATE INDEX IF NOT EXISTS idx_accounts_cb_status ON accounts(cb_status);
CREATE INDEX IF NOT EXISTS idx_accounts_gl_status ON accounts(gl_status);
CREATE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);
CREATE INDEX IF NOT EXISTS idx_usage_logs_created ON usage_logs(created_at);

CREATE TABLE IF NOT EXISTS settings (
    key     TEXT PRIMARY KEY,
    value   TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS proxies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    url         TEXT NOT NULL,
    label       TEXT DEFAULT '',
    type        TEXT DEFAULT 'http',       -- http, socks5
    active      INTEGER DEFAULT 1,
    last_latency_ms INTEGER DEFAULT -1,    -- -1 = untested
    last_tested TEXT DEFAULT '',
    last_error  TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS filters (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    find_text    TEXT NOT NULL,
    replace_text TEXT NOT NULL DEFAULT '',
    is_regex     INTEGER NOT NULL DEFAULT 0,
    enabled      INTEGER NOT NULL DEFAULT 1,
    description  TEXT DEFAULT '',
    hit_count    INTEGER DEFAULT 0,
    created_at   TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS vps_servers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host        TEXT NOT NULL,
    ssh_port    INTEGER NOT NULL DEFAULT 22,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,
    label       TEXT DEFAULT '',
    status      TEXT DEFAULT 'unknown',  -- unknown, online, offline, installed
    os_info     TEXT DEFAULT '',
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS mcp_instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_path  TEXT NOT NULL,
    port            INTEGER NOT NULL DEFAULT 9876,
    pid             INTEGER DEFAULT 0,
    status          TEXT DEFAULT 'stopped',  -- stopped, running
    tunnel_url      TEXT DEFAULT '',
    tunnel_pid      INTEGER DEFAULT 0,
    created_at      TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_sessions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT DEFAULT 'New Chat',
    model       TEXT DEFAULT '',
    endpoint    TEXT DEFAULT '',
    api_key     TEXT DEFAULT '',
    opencode_session_key TEXT DEFAULT '',
    gumloop_account_id INTEGER DEFAULT 0,
    gumloop_interaction_id TEXT DEFAULT '',
    last_gumloop_account_id INTEGER DEFAULT 0,
    created_at  TEXT DEFAULT (datetime('now')),
    updated_at  TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    opencode_message_id TEXT DEFAULT '',
    role TEXT NOT NULL,                       -- system, user, assistant, thinking
    content TEXT NOT NULL DEFAULT '',
    model TEXT DEFAULT '',
    created_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS gumloop_interaction_bindings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id INTEGER NOT NULL,
    account_id INTEGER NOT NULL,
    interaction_id TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_session_id, account_id)
);

CREATE INDEX IF NOT EXISTS idx_gl_bindings_session ON gumloop_interaction_bindings(chat_session_id);
CREATE INDEX IF NOT EXISTS idx_gl_bindings_account ON gumloop_interaction_bindings(account_id);

CREATE TABLE IF NOT EXISTS gumloop_session_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_session_id INTEGER NOT NULL,
    summary_text TEXT NOT NULL DEFAULT '',
    watermark_message_id INTEGER NOT NULL DEFAULT 0,
    message_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(chat_session_id)
);

CREATE INDEX IF NOT EXISTS idx_gl_summaries_session ON gumloop_session_summaries(chat_session_id);
"""


async def _run_migrations(conn: aiosqlite.Connection) -> None:
    """Add new columns to existing tables (idempotent)."""
    migrations = [
        # usage_logs new columns
        "ALTER TABLE usage_logs ADD COLUMN request_headers TEXT DEFAULT ''",
        "ALTER TABLE usage_logs ADD COLUMN request_body TEXT DEFAULT ''",
        "ALTER TABLE usage_logs ADD COLUMN response_headers TEXT DEFAULT ''",
        "ALTER TABLE usage_logs ADD COLUMN response_body TEXT DEFAULT ''",
        "ALTER TABLE usage_logs ADD COLUMN error_message TEXT DEFAULT ''",
        # accounts new columns
        "ALTER TABLE accounts ADD COLUMN cb_expires_at TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN kiro_credits_total REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN kiro_credits_used REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN kiro_expires_at TEXT DEFAULT ''",
        # WaveSpeed columns
        "ALTER TABLE accounts ADD COLUMN ws_status TEXT DEFAULT 'none'",
        "ALTER TABLE accounts ADD COLUMN ws_api_key TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN ws_credits REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN ws_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN ws_error_count INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN last_used_ws TEXT DEFAULT ''",
        # Verified flags: 0=temporary (needs review), 1=confirmed fix
        "ALTER TABLE accounts ADD COLUMN kiro_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN cb_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN ws_verified INTEGER DEFAULT 0",
        # Gumloop chat session persistence
        "ALTER TABLE chat_sessions ADD COLUMN opencode_session_key TEXT DEFAULT ''",
        "ALTER TABLE chat_sessions ADD COLUMN gumloop_account_id INTEGER DEFAULT 0",
        "ALTER TABLE chat_sessions ADD COLUMN gumloop_interaction_id TEXT DEFAULT ''",
        "ALTER TABLE chat_sessions ADD COLUMN last_gumloop_account_id INTEGER DEFAULT 0",
        "ALTER TABLE chat_messages ADD COLUMN opencode_message_id TEXT DEFAULT ''",
        # Last test error for review
        "ALTER TABLE accounts ADD COLUMN kiro_test_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN cb_test_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN ws_test_error TEXT DEFAULT ''",
        # Gumloop columns
        "ALTER TABLE accounts ADD COLUMN gl_status TEXT DEFAULT 'none'",
        "ALTER TABLE accounts ADD COLUMN gl_refresh_token TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_user_id TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_gummie_id TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_id_token TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_credits REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN gl_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_error_count INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN last_used_gl TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN gl_test_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN gl_exhausted_until TEXT DEFAULT ''",
        # ChatBAI fields
        "ALTER TABLE accounts ADD COLUMN cbai_status TEXT DEFAULT 'none'",
        "ALTER TABLE accounts ADD COLUMN cbai_api_key TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN cbai_session_token TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN cbai_credits REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN cbai_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN cbai_error_count INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN last_used_cbai TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN cbai_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN cbai_test_error TEXT DEFAULT ''",
        # SkillBoss fields
        "ALTER TABLE accounts ADD COLUMN skboss_status TEXT DEFAULT 'none'",
        "ALTER TABLE accounts ADD COLUMN skboss_api_key TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN skboss_credits REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN skboss_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN skboss_error_count INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN last_used_skboss TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN skboss_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN skboss_test_error TEXT DEFAULT ''",
        # Windsurf fields
        "ALTER TABLE accounts ADD COLUMN windsurf_status TEXT DEFAULT 'none'",
        "ALTER TABLE accounts ADD COLUMN windsurf_api_key TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN windsurf_credits REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN windsurf_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN windsurf_error_count INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN last_used_windsurf TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN windsurf_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN windsurf_test_error TEXT DEFAULT ''",
        # TheRouter fields
        "ALTER TABLE accounts ADD COLUMN tr_status TEXT DEFAULT 'none'",
        "ALTER TABLE accounts ADD COLUMN tr_api_key TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN tr_credits REAL DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN tr_error TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN tr_error_count INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN last_used_tr TEXT DEFAULT ''",
        "ALTER TABLE accounts ADD COLUMN tr_verified INTEGER DEFAULT 0",
        "ALTER TABLE accounts ADD COLUMN tr_test_error TEXT DEFAULT ''",
        # Proxy pool separation + selection
        "ALTER TABLE proxies ADD COLUMN purpose TEXT DEFAULT 'api'",
        "ALTER TABLE proxies ADD COLUMN checked INTEGER DEFAULT 0",
        # Proxy URL logging on usage_logs
        "ALTER TABLE usage_logs ADD COLUMN proxy_url TEXT DEFAULT ''",
        # OpenCode session registry
        "ALTER TABLE opencode_session_registry ADD COLUMN last_model TEXT DEFAULT ''",
        # Delta rehydration watermark for Gumloop bindings
        "ALTER TABLE gumloop_interaction_bindings ADD COLUMN last_synced_message_id INTEGER DEFAULT NULL",
        # Gumloop session summaries table (created via schema but migration ensures it exists on upgrade)
        """CREATE TABLE IF NOT EXISTS gumloop_session_summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_session_id INTEGER NOT NULL,
            summary_text TEXT NOT NULL DEFAULT '',
            watermark_message_id INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            UNIQUE(chat_session_id)
        )""",
    ]
    for sql in migrations:
        try:
            await conn.execute(sql)
        except Exception:
            pass  # Column already exists
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_sessions_gumloop_account "
            "ON chat_sessions(gumloop_account_id) WHERE gumloop_account_id > 0"
        )
    except Exception:
        pass
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_sessions_opencode_session_key "
            "ON chat_sessions(opencode_session_key) WHERE opencode_session_key <> ''"
        )
    except Exception:
        pass
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_opencode_session_registry_api_key_id "
            "ON opencode_session_registry(api_key_id)"
        )
    except Exception:
        pass
    try:
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_chat_messages_opencode_msg "
            "ON chat_messages(session_id, opencode_message_id) WHERE opencode_message_id <> ''"
        )
    except Exception:
        pass
    await conn.commit()


async def _seed_settings(conn: aiosqlite.Connection) -> None:
    """Seed default settings if not present (idempotent)."""
    defaults = {
        "batch_proxy_enabled": "false",
        "batch_smart_rotate": "false",
        "api_proxy_smart_rotate": "false",
        "sticky_account_standard": "",
        "sticky_account_max": "",
        "sticky_account_wavespeed": "",
        "sticky_account_max_gl": "",
        "sticky_account_chatbai": "",
        "sticky_account_skillboss": "",
        "sticky_account_windsurf": "",
    }
    for key, value in defaults.items():
        await conn.execute(
            "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
    await conn.commit()


async def init_db() -> None:
    db = await get_db()
    await db.executescript(_SCHEMA)
    await db.commit()
    await _run_migrations(db)
    await _seed_settings(db)


# ---------------------------------------------------------------------------
# Settings (key-value store)
# ---------------------------------------------------------------------------

async def get_setting(key: str, default: str = "") -> str:
    db = await get_db()
    cur = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    row = await cur.fetchone()
    return row["value"] if row else default


async def set_setting(key: str, value: str) -> None:
    db = await get_db()
    await db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = ?",
        (key, value, value),
    )
    await db.commit()


async def get_proxy_config() -> dict:
    """Return proxy config: {enabled: bool, url: str (best active proxy)}."""
    enabled = (await get_setting("proxy_enabled", "false")).lower() in ("true", "1")
    if not enabled:
        return {"enabled": False, "url": ""}
    # Pick best active proxy (lowest latency, tested OK)
    db = await get_db()
    cur = await db.execute(
        """SELECT url FROM proxies WHERE active = 1 AND last_latency_ms > 0
           ORDER BY last_latency_ms ASC LIMIT 1"""
    )
    row = await cur.fetchone()
    if row:
        return {"enabled": True, "url": row["url"]}
    # Fallback: any active proxy
    cur = await db.execute("SELECT url FROM proxies WHERE active = 1 LIMIT 1")
    row = await cur.fetchone()
    return {"enabled": True, "url": row["url"] if row else ""}


async def set_proxy_config(enabled: bool, url: str = "") -> None:
    await set_setting("proxy_enabled", "true" if enabled else "false")
    if url:
        await set_setting("proxy_url", url)


async def get_all_active_proxies() -> list[dict]:
    """Return all active proxies ordered by latency."""
    db = await get_db()
    cur = await db.execute(
        "SELECT * FROM proxies WHERE active = 1 ORDER BY last_latency_ms ASC"
    )
    return [dict(r) for r in await cur.fetchall()]


# ---------------------------------------------------------------------------
# Proxy CRUD
# ---------------------------------------------------------------------------

async def add_proxy(url: str, label: str = "", proxy_type: str = "http",
                    purpose: str = "api") -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO proxies (url, label, type, purpose) VALUES (?, ?, ?, ?)",
        (url, label, proxy_type, purpose),
    )
    await db.commit()
    return cur.lastrowid


async def get_proxies(purpose: str | None = None) -> list[dict]:
    db = await get_db()
    if purpose:
        cur = await db.execute(
            "SELECT * FROM proxies WHERE purpose = ? ORDER BY active DESC, last_latency_ms ASC",
            (purpose,),
        )
    else:
        cur = await db.execute("SELECT * FROM proxies ORDER BY active DESC, last_latency_ms ASC")
    return [dict(r) for r in await cur.fetchall()]


async def delete_proxy(proxy_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM proxies WHERE id = ?", (proxy_id,))
    await db.commit()
    return cur.rowcount > 0


async def toggle_proxy(proxy_id: int, active: bool) -> bool:
    db = await get_db()
    cur = await db.execute(
        "UPDATE proxies SET active = ? WHERE id = ?", (1 if active else 0, proxy_id)
    )
    await db.commit()
    return cur.rowcount > 0


_rotate_index = 0


async def get_next_proxy_url() -> str | None:
    """Smart rotate: return next active proxy URL in round-robin order.
    Returns None if proxy disabled or no active proxies.
    """
    global _rotate_index
    enabled = (await get_setting("proxy_enabled", "false")).lower() in ("true", "1")
    if not enabled:
        return None
    proxies = await get_all_active_proxies()
    if not proxies:
        return None
    _rotate_index = (_rotate_index + 1) % len(proxies)
    return proxies[_rotate_index]["url"]


async def get_proxy_with_fallback() -> list[str]:
    """Return list of active proxy URLs for fallback (ordered by latency)."""
    enabled = (await get_setting("proxy_enabled", "false")).lower() in ("true", "1")
    if not enabled:
        return []
    proxies = await get_all_active_proxies()
    return [p["url"] for p in proxies if p["url"]]


async def update_proxy_test(proxy_id: int, latency_ms: int, error: str = "") -> None:
    db = await get_db()
    await db.execute(
        "UPDATE proxies SET last_latency_ms = ?, last_tested = datetime('now'), last_error = ? WHERE id = ?",
        (latency_ms, error, proxy_id),
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Proxy pool: checked selection + smart rotate
# ---------------------------------------------------------------------------

async def get_checked_proxies(purpose: str) -> list[dict]:
    """Return checked + active proxies for a purpose, ordered by latency."""
    db = await get_db()
    cur = await db.execute(
        """SELECT * FROM proxies
           WHERE checked = 1 AND active = 1 AND purpose = ?
           ORDER BY last_latency_ms ASC""",
        (purpose,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def toggle_proxy_checked(proxy_id: int, checked: bool, purpose: str) -> bool:
    """Toggle checked flag for a proxy. Checkboxes are always free — mode determines runtime behavior."""
    db = await get_db()
    await db.execute(
        "UPDATE proxies SET checked = ? WHERE id = ?",
        (1 if checked else 0, proxy_id),
    )
    await db.commit()
    return True


_api_rotate_idx = 0
_batch_rotate_idx = 0


async def _get_proxy_for_purpose(purpose: str) -> dict | None:
    """Get the next proxy for a purpose. Handles sticky vs rotate mode.

    Sticky: use first checked proxy. If it fails (latency=-1), skip to next.
    Rotate: round-robin across all checked proxies.
    """
    global _api_rotate_idx, _batch_rotate_idx
    setting_enabled = "proxy_enabled" if purpose == "api" else "batch_proxy_enabled"
    setting_smart = "api_proxy_smart_rotate" if purpose == "api" else "batch_smart_rotate"

    enabled = (await get_setting(setting_enabled, "false")).lower() in ("true", "1")
    if not enabled:
        return None
    proxies = await get_checked_proxies(purpose)
    if not proxies:
        return None

    smart = (await get_setting(setting_smart, "false")).lower() in ("true", "1")
    if smart and len(proxies) > 1:
        # Rotate mode: round-robin
        if purpose == "api":
            _api_rotate_idx = (_api_rotate_idx + 1) % len(proxies)
            p = proxies[_api_rotate_idx]
        else:
            _batch_rotate_idx = (_batch_rotate_idx + 1) % len(proxies)
            p = proxies[_batch_rotate_idx]
    else:
        # Sticky mode: use first checked. If it's failed (latency=-1 + tested), try next.
        p = proxies[0]
        for candidate in proxies:
            if candidate.get("last_latency_ms", 0) != -1 or not candidate.get("last_tested"):
                p = candidate
                break
    return {"url": p["url"], "id": p["id"]}


async def get_proxy_for_api_call() -> dict | None:
    """Get the next proxy for an API call."""
    return await _get_proxy_for_purpose("api")


async def get_proxy_for_batch() -> dict | None:
    """Get the next proxy for a batch login. Only uses batch-purpose proxies."""
    return await _get_proxy_for_purpose("batch")


async def get_batch_proxies_for_workers(n: int) -> list[dict]:
    """Get N unique proxies for concurrent batch workers.

    Returns up to N proxies from the checked batch pool.
    Falls back to API proxy pool if batch pool is empty.
    Each worker gets a different proxy. If no proxies at all, returns [None]*n.
    """
    proxies = await get_checked_proxies("batch")
    if not proxies:
        return [None] * n  # type: ignore — no proxies, workers run without proxy
    # Cycle through proxies if n > len(proxies)
    result = []
    for i in range(n):
        result.append({"url": proxies[i % len(proxies)]["url"], "id": proxies[i % len(proxies)]["id"]})
    return result


# ---------------------------------------------------------------------------
# Sticky account helpers
# ---------------------------------------------------------------------------

async def get_sticky_account_id(tier: str) -> int | None:
    """Read the sticky account ID for a tier from settings."""
    val = await get_setting(f"sticky_account_{tier}", "")
    if val and val.isdigit():
        return int(val)
    return None


async def is_sticky_pinned(tier: str) -> bool:
    """Check if the sticky account was manually pinned by user (don't auto-clear)."""
    val = await get_setting(f"sticky_pinned_{tier}", "")
    return val == "1"


async def set_sticky_account(tier: str, account_id: int, pinned: bool = False) -> None:
    """Set the sticky account for a tier. If pinned=True, won't be auto-cleared on errors."""
    await set_setting(f"sticky_account_{tier}", str(account_id))
    if pinned:
        await set_setting(f"sticky_pinned_{tier}", "1")


async def clear_sticky_account(tier: str) -> None:
    """Clear the sticky account for a tier, forcing rotation to next.

    Respects pinned accounts — if pinned, does NOT clear.
    Use force_clear_sticky_account() to override.
    """
    if await is_sticky_pinned(tier):
        return  # Don't auto-clear pinned accounts
    await set_setting(f"sticky_account_{tier}", "")


async def force_clear_sticky_account(tier: str) -> None:
    """Force-clear sticky account, even if pinned. Used by admin UI."""
    await set_setting(f"sticky_account_{tier}", "")
    await set_setting(f"sticky_pinned_{tier}", "")


# ---------------------------------------------------------------------------
# Gumloop temporary exhaustion
# ---------------------------------------------------------------------------

async def mark_gl_exhausted_temporary(account_id: int, cooldown_seconds: int, error: str = "") -> None:
    """Mark a Gumloop account as temporarily exhausted with a cooldown.

    After cooldown_seconds, the account will auto-recover to 'ok' on next rotation check.
    """
    from datetime import datetime, timezone, timedelta
    until = (datetime.now(timezone.utc) + timedelta(seconds=cooldown_seconds)).strftime("%Y-%m-%d %H:%M:%S")
    await update_account(
        account_id,
        gl_status="exhausted",
        gl_error=error[:200] if error else "",
        gl_exhausted_until=until,
    )
    await clear_sticky_account("max_gl")
    log.info("GL account %d marked exhausted until %s (%ds cooldown): %s",
             account_id, until, cooldown_seconds, error[:80])


async def recover_gl_exhausted_accounts() -> int:
    """Auto-recover GL accounts whose cooldown has expired. Returns count recovered."""
    db = await get_db()
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    cur = await db.execute(
        """SELECT id, email FROM accounts
           WHERE status = 'active'
             AND gl_status = 'exhausted'
             AND gl_exhausted_until != ''
             AND gl_exhausted_until <= ?""",
        (now,),
    )
    rows = await cur.fetchall()
    count = 0
    for row in rows:
        await db.execute(
            """UPDATE accounts
               SET gl_status = 'ok', gl_error = '', gl_error_count = 0, gl_exhausted_until = ''
               WHERE id = ?""",
            (row["id"],),
        )
        count += 1
        log.info("GL account %s auto-recovered from temporary exhaustion", row["email"])
    if count:
        await db.commit()
    return count


# ---------------------------------------------------------------------------
# API key helpers
# ---------------------------------------------------------------------------

def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> str:
    return "sk-" + secrets.token_hex(24)


# ---------------------------------------------------------------------------
# API key CRUD
# ---------------------------------------------------------------------------

async def create_api_key(name: str = "default") -> tuple[int, str]:
    """Create a new API key. Returns (id, full_key)."""
    db = await get_db()
    full_key = generate_api_key()
    h = hash_key(full_key)
    prefix = full_key[:7] + "..."
    cur = await db.execute(
        "INSERT INTO api_keys (key_hash, key_prefix, name) VALUES (?, ?, ?)",
        (h, prefix, name),
    )
    await db.commit()
    return cur.lastrowid, full_key


async def verify_api_key(key: str) -> Optional[dict]:
    """Verify an API key. Returns key row dict or None."""
    db = await get_db()
    h = hash_key(key)
    cur = await db.execute(
        "SELECT * FROM api_keys WHERE key_hash = ? AND active = 1", (h,)
    )
    row = await cur.fetchone()
    if row is None:
        return None
    # Update last_used and usage_count
    await db.execute(
        "UPDATE api_keys SET last_used = datetime('now'), usage_count = usage_count + 1 WHERE id = ?",
        (row["id"],),
    )
    await db.commit()
    return dict(row)


async def get_api_keys() -> list[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM api_keys ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def revoke_api_key(key_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
    await db.commit()
    return cur.rowcount > 0


async def regenerate_api_key(key_id: int) -> Optional[str]:
    """Regenerate an API key. Returns new full key or None."""
    db = await get_db()
    cur = await db.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    row = await cur.fetchone()
    if row is None:
        return None
    new_key = generate_api_key()
    h = hash_key(new_key)
    prefix = new_key[:7] + "..."
    await db.execute(
        "UPDATE api_keys SET key_hash = ?, key_prefix = ?, last_used = '' WHERE id = ?",
        (h, prefix, key_id),
    )
    await db.commit()
    return new_key


async def count_active_api_keys() -> int:
    db = await get_db()
    cur = await db.execute("SELECT COUNT(*) as cnt FROM api_keys WHERE active = 1")
    row = await cur.fetchone()
    return row["cnt"]


# ---------------------------------------------------------------------------
# D1 auto-push hook (registered by license_client on startup)
# ---------------------------------------------------------------------------

import asyncio as _asyncio
from typing import Callable, Coroutine

# Callback: async fn(account_id: int) -> None — pushes account to D1
_d1_push_hook: Optional[Callable[[int], Coroutine]] = None
# Callback: async fn(email: str) -> None — deletes account from D1
_d1_delete_hook: Optional[Callable[[str], Coroutine]] = None
# Flag to suppress auto-push (set True during pull-from-D1 writes)
_suppress_auto_push: bool = False


def register_d1_hooks(
    push_hook: Callable[[int], Coroutine],
    delete_hook: Callable[[str], Coroutine],
) -> None:
    """Register D1 sync hooks. Called by license_client after activation."""
    global _d1_push_hook, _d1_delete_hook
    _d1_push_hook = push_hook
    _d1_delete_hook = delete_hook
    log.info("D1 auto-push hooks registered")


def _fire_push(account_id: int) -> None:
    """Fire-and-forget D1 push for an account. Non-blocking."""
    if _suppress_auto_push or _d1_push_hook is None:
        return
    try:
        _asyncio.get_event_loop().create_task(_safe_push(account_id))
    except RuntimeError:
        pass  # No event loop — skip (e.g. during shutdown)


async def _safe_push(account_id: int) -> None:
    """Push account to D1, swallowing errors."""
    try:
        await _d1_push_hook(account_id)
    except Exception as e:
        log.debug("D1 auto-push failed for account %d: %s", account_id, e)


def _fire_delete(email: str) -> None:
    """Fire-and-forget D1 delete for an account. Non-blocking."""
    if _suppress_auto_push or _d1_delete_hook is None:
        return
    try:
        _asyncio.get_event_loop().create_task(_safe_delete(email))
    except RuntimeError:
        pass


async def _safe_delete(email: str) -> None:
    """Delete account from D1, swallowing errors."""
    try:
        await _d1_delete_hook(email)
    except Exception as e:
        log.debug("D1 auto-delete failed for %s: %s", email, e)


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------

async def create_account(email: str, password: str) -> int:
    """Create account in local DB and auto-push to D1."""
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO accounts (email, password) VALUES (?, ?)",
        (email, password),
    )
    await db.commit()
    account_id = cur.lastrowid
    _fire_push(account_id)
    return account_id


async def get_accounts(status: Optional[str] = None) -> list[dict]:
    db = await get_db()
    if status:
        cur = await db.execute(
            "SELECT * FROM accounts WHERE status = ? ORDER BY created_at DESC",
            (status,),
        )
    else:
        cur = await db.execute("SELECT * FROM accounts ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def get_account(account_id: int) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM accounts WHERE id = ?", (account_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_account(account_id: int, **fields: Any) -> bool:
    """Update account in local DB and auto-push to D1."""
    if not fields:
        return False
    db = await get_db()
    fields["updated_at"] = "datetime('now')"
    sets = []
    vals = []
    for k, v in fields.items():
        if v == "datetime('now')":
            sets.append(f"{k} = datetime('now')")
        else:
            sets.append(f"{k} = ?")
            vals.append(v)
    vals.append(account_id)
    await db.execute(
        f"UPDATE accounts SET {', '.join(sets)} WHERE id = ?", vals
    )
    await db.commit()
    _fire_push(account_id)
    return True


async def deduct_cb_credit(account_id: int, amount: float = 1.0) -> None:
    """Deduct credits from a CodeBuddy account. Don't auto-exhausted — let upstream decide."""
    db = await get_db()
    await db.execute(
        "UPDATE accounts SET cb_credits = MAX(0, cb_credits - ?) WHERE id = ?",
        (amount, account_id),
    )
    await db.commit()


CB_DEFAULT_CREDITS = 250.0


async def deduct_ws_credit(account_id: int, cost: float) -> None:
    """Deduct cost from WaveSpeed account. Don't auto-exhausted — let upstream decide."""
    if cost <= 0:
        return
    db = await get_db()
    await db.execute(
        "UPDATE accounts SET ws_credits = MAX(0, ws_credits - ?) WHERE id = ?",
        (cost, account_id),
    )
    await db.commit()


async def deduct_cbai_credit(account_id: int, cost: float) -> None:
    """Deduct cost from ChatBAI account."""
    if cost <= 0:
        return
    db = await get_db()
    await db.execute(
        "UPDATE accounts SET cbai_credits = MAX(0, cbai_credits - ?) WHERE id = ?",
        (cost, account_id),
    )
    await db.commit()


async def deduct_skboss_credit(account_id: int, cost: float) -> None:
    """Deduct cost from SkillBoss account."""
    if cost <= 0:
        return
    db = await get_db()
    await db.execute(
        "UPDATE accounts SET skboss_credits = MAX(0, skboss_credits - ?) WHERE id = ?",
        (cost, account_id),
    )
    await db.commit()


async def deduct_windsurf_credit(account_id: int, cost: float) -> None:
    """Deduct cost from Windsurf account."""
    if cost <= 0:
        return
    db = await get_db()
    await db.execute(
        "UPDATE accounts SET windsurf_credits = MAX(0, windsurf_credits - ?) WHERE id = ?",
        (cost, account_id),
    )
    await db.commit()


async def delete_account(account_id: int) -> bool:
    """Delete account from local DB and auto-push delete to D1."""
    db = await get_db()
    # Get email before deleting (needed for D1 delete)
    cur = await db.execute("SELECT email FROM accounts WHERE id = ?", (account_id,))
    row = await cur.fetchone()
    email = row["email"] if row else None

    cur = await db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))
    await db.commit()
    deleted = cur.rowcount > 0

    if deleted and email:
        _fire_delete(email)

    return deleted


async def move_to_trash(account_id: int) -> bool:
    return await update_account(account_id, status="trash")


async def restore_account(account_id: int) -> bool:
    return await update_account(account_id, status="active")


async def get_failed() -> list[dict]:
    return await get_accounts(status="failed")


async def get_next_account_for_tier(tier: str, exclude_ids: list[int] | None = None) -> Optional[dict]:
    """Get the next available account for the given tier.

    Supports exclude_ids for retry loops — skip already-tried accounts.
    Pinned accounts returned first (even if errored), then auto-rotation.
    For max_gl tier: auto-recovers temporarily exhausted accounts whose cooldown expired.
    """
    db = await get_db()
    tier_config = {
        "standard": ("kiro_status", "last_used_kiro"),
        "max": ("cb_status", "last_used_cb"),
        "wavespeed": ("ws_status", "last_used_ws"),
        "max_gl": ("gl_status", "last_used_gl"),
        "chatbai": ("cbai_status", "last_used_cbai"),
        "skillboss": ("skboss_status", "last_used_skboss"),
        "windsurf": ("windsurf_status", "last_used_windsurf"),
        "therouter": ("tr_status", "last_used_tr"),
    }
    status_col, last_used_col = tier_config.get(tier, ("kiro_status", "last_used_kiro"))
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    skip = set(exclude_ids or [])

    # Auto-recover temporarily exhausted GL accounts whose cooldown has expired
    if tier == "max_gl":
        await recover_gl_exhausted_accounts()

    # 1. Check sticky/pinned account (only if not excluded)
    sticky_id = await get_sticky_account_id(tier)
    if sticky_id and sticky_id not in skip:
        account = await get_account(sticky_id)
        pinned = await is_sticky_pinned(tier)

        if account and account["status"] == "active":
            if account[status_col] == "ok":
                await db.execute(
                    f"UPDATE accounts SET {last_used_col} = ? WHERE id = ?",
                    (ts, sticky_id),
                )
                await db.commit()
                return account
            elif pinned:
                await db.execute(
                    f"UPDATE accounts SET {last_used_col} = ? WHERE id = ?",
                    (ts, sticky_id),
                )
                await db.commit()
                return account

        if not pinned:
            await clear_sticky_account(tier)

    # 2. Pick next active+ok account, excluding already-tried ones
    if skip:
        placeholders = ",".join("?" for _ in skip)
        cur = await db.execute(
            f"""SELECT * FROM accounts
                WHERE status = 'active' AND {status_col} = 'ok'
                AND id NOT IN ({placeholders})
                ORDER BY created_at ASC, id ASC
                LIMIT 1""",
            list(skip),
        )
    else:
        cur = await db.execute(
            f"""SELECT * FROM accounts
                WHERE status = 'active' AND {status_col} = 'ok'
                ORDER BY created_at ASC, id ASC
                LIMIT 1"""
        )
    row = await cur.fetchone()
    if row is None:
        return None

    # 3. Set as new sticky (auto, not pinned)
    await set_sticky_account(tier, row["id"], pinned=False)
    await db.execute(
        f"UPDATE accounts SET {last_used_col} = ? WHERE id = ?",
        (ts, row["id"]),
    )
    await db.commit()
    return dict(row)


# ---------------------------------------------------------------------------
# Usage logging — IN-MEMORY (temporary, cleared on restart)
# ---------------------------------------------------------------------------

_usage_logs: list[dict] = []
_usage_log_id_counter = 0
_MAX_USAGE_LOGS = 500  # Keep last 500 logs in memory


async def log_usage(
    api_key_id: Optional[int],
    account_id: Optional[int],
    model: str,
    tier: str,
    status_code: int = 200,
    latency_ms: int = 0,
    request_headers: str = "",
    request_body: str = "",
    response_headers: str = "",
    response_body: str = "",
    error_message: str = "",
    proxy_url: str = "",
) -> int:
    global _usage_log_id_counter
    _usage_log_id_counter += 1

    # Resolve account email
    account_email = ""
    if account_id:
        acc = await get_account(account_id)
        if acc:
            account_email = acc.get("email", "")

    entry = {
        "id": _usage_log_id_counter,
        "api_key_id": api_key_id,
        "account_id": account_id,
        "account_email": account_email,
        "model": model,
        "tier": tier,
        "status_code": status_code,
        "latency_ms": latency_ms,
        "request_headers": request_headers,
        "request_body": request_body,
        "response_headers": response_headers,
        "response_body": response_body,
        "error_message": error_message,
        "proxy_url": proxy_url,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _usage_logs.append(entry)

    # Trim to max size
    if len(_usage_logs) > _MAX_USAGE_LOGS:
        _usage_logs[:] = _usage_logs[-_MAX_USAGE_LOGS:]

    # Buffer for central sync (lightweight metadata only)
    try:
        from . import license_client
        license_client.buffer_usage_log(
            model=model, tier=tier, status_code=status_code,
            latency_ms=latency_ms, proxy_url=proxy_url,
            error_message=error_message, account_email=account_email,
        )
    except Exception:
        pass  # License client not initialized or import error — ignore

    return _usage_log_id_counter


async def get_usage_stats() -> dict:
    db = await get_db()
    total = len(_usage_logs)

    by_tier: dict[str, int] = {}
    by_model: dict[str, int] = {}
    success_count = 0
    total_latency = 0
    latency_count = 0

    for log in _usage_logs:
        t = log["tier"]
        m = log["model"]
        by_tier[t] = by_tier.get(t, 0) + 1
        by_model[m] = by_model.get(m, 0) + 1
        if 200 <= log["status_code"] < 400:
            success_count += 1
            total_latency += log["latency_ms"]
            latency_count += 1

    success_rate = (success_count / total * 100) if total > 0 else 0
    avg_latency = (total_latency / latency_count) if latency_count > 0 else 0

    # Sort by_model by count desc, limit 50
    by_model = dict(sorted(by_model.items(), key=lambda x: -x[1])[:50])

    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM accounts WHERE status='active' AND kiro_status='ok'"
    )
    kiro_active = (await cur.fetchone())["cnt"]

    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM accounts WHERE status='active' AND cb_status='ok'"
    )
    cb_active = (await cur.fetchone())["cnt"]

    cur = await db.execute(
        "SELECT COUNT(*) as cnt FROM accounts WHERE status='active' AND gl_status='ok'"
    )
    gl_active = (await cur.fetchone())["cnt"]

    cur = await db.execute("SELECT COUNT(*) as cnt FROM accounts")
    total_accounts = (await cur.fetchone())["cnt"]

    active_keys = await count_active_api_keys()

    recent = _usage_logs[-20:][::-1]

    return {
        "total_requests": total,
        "success_rate": round(success_rate, 1),
        "avg_latency": round(avg_latency),
        "requests_by_tier": by_tier,
        "requests_by_model": by_model,
        "by_model": by_model,
        "recent_requests": recent,
        "active_accounts_kiro": kiro_active,
        "active_accounts_cb": cb_active,
        "active_accounts_gl": gl_active,
        "total_accounts": total_accounts,
        "api_keys_active": active_keys,
    }


async def get_usage_logs(limit: int = 50) -> list[dict]:
    """Return recent usage logs (in-memory, newest first)."""
    return _usage_logs[-limit:][::-1]


async def get_usage_log(log_id: int) -> Optional[dict]:
    """Return a single usage log by ID."""
    for log in _usage_logs:
        if log["id"] == log_id:
            return log
    return None


async def update_account_credits(
    account_id: int,
    kiro_credits: Optional[float] = None,
    kiro_credits_total: Optional[float] = None,
    kiro_credits_used: Optional[float] = None,
    kiro_status: Optional[str] = None,
    cb_credits: Optional[float] = None,
    cb_status: Optional[str] = None,
) -> bool:
    """Update credit-related fields for an account."""
    fields: dict[str, Any] = {}
    if kiro_credits is not None:
        fields["kiro_credits"] = kiro_credits
    if kiro_credits_total is not None:
        fields["kiro_credits_total"] = kiro_credits_total
    if kiro_credits_used is not None:
        fields["kiro_credits_used"] = kiro_credits_used
    if kiro_status is not None:
        fields["kiro_status"] = kiro_status
    if cb_credits is not None:
        fields["cb_credits"] = cb_credits
    if cb_status is not None:
        fields["cb_status"] = cb_status
    if not fields:
        return False
    return await update_account(account_id, **fields)


async def get_account_by_email(email: str) -> Optional[dict]:
    db = await get_db()
    cur = await db.execute("SELECT * FROM accounts WHERE email = ?", (email,))
    row = await cur.fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Filter rules CRUD
# ---------------------------------------------------------------------------

async def get_filters(enabled_only: bool = False) -> list[dict]:
    db = await get_db()
    if enabled_only:
        cur = await db.execute(
            "SELECT * FROM filters WHERE enabled = 1 ORDER BY id ASC"
        )
    else:
        cur = await db.execute("SELECT * FROM filters ORDER BY id ASC")
    return [dict(r) for r in await cur.fetchall()]


async def create_filter(
    find_text: str,
    replace_text: str = "",
    is_regex: bool = False,
    description: str = "",
) -> int:
    db = await get_db()
    cur = await db.execute(
        "INSERT INTO filters (find_text, replace_text, is_regex, description) VALUES (?, ?, ?, ?)",
        (find_text, replace_text, 1 if is_regex else 0, description),
    )
    await db.commit()
    return cur.lastrowid


async def update_filter(filter_id: int, **fields: Any) -> bool:
    if not fields:
        return False
    db = await get_db()
    sets = []
    vals = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(filter_id)
    cur = await db.execute(
        f"UPDATE filters SET {', '.join(sets)} WHERE id = ?", vals
    )
    await db.commit()
    return cur.rowcount > 0


async def delete_filter(filter_id: int) -> bool:
    db = await get_db()
    cur = await db.execute("DELETE FROM filters WHERE id = ?", (filter_id,))
    await db.commit()
    return cur.rowcount > 0


async def toggle_filter(filter_id: int, enabled: bool) -> bool:
    return await update_filter(filter_id, enabled=1 if enabled else 0)


async def increment_filter_hit(filter_id: int) -> None:
    db = await get_db()
    await db.execute(
        "UPDATE filters SET hit_count = hit_count + 1 WHERE id = ?",
        (filter_id,),
    )
    await db.commit()


async def seed_default_filters(force: bool = False) -> int:
    """Seed default filter rules. Returns count seeded."""
    db = await get_db()
    if force:
        await db.execute("DELETE FROM filters")
        await db.commit()
    else:
        cur = await db.execute("SELECT COUNT(*) as cnt FROM filters")
        row = await cur.fetchone()
        if row["cnt"] > 0:
            return 0

    # (find_text, replace_text, description)
    defaults = [
        ("Powerful AI Agent", "Advance Ai Agent", "Replace Powerful AI Agent"),
        ("powerful AI agent", "advance Ai agent", "Replace powerful AI agent"),
        ("powerful ai agent", "advance ai agent", "Replace powerful ai agent"),
        ("Advanced AI Agent", "Advance Ai Agent", "Replace Advanced AI Agent"),
        ("advanced AI agent", "advance Ai agent", "Replace advanced AI agent"),
        ("advanced ai agent", "advance ai agent", "Replace advanced ai agent"),
    ]
    count = 0
    for find, replace, desc in defaults:
        await db.execute(
            "INSERT INTO filters (find_text, replace_text, description) VALUES (?, ?, ?)",
            (find, replace, desc),
        )
        count += 1
    await db.commit()
    return count


# ---------------------------------------------------------------------------
# VPS Servers CRUD
# ---------------------------------------------------------------------------

async def add_vps_server(
    host: str, username: str, password: str,
    ssh_port: int = 22, label: str = "", os_info: str = "",
) -> int:
    """Add a VPS server. Returns ID."""
    conn = await get_db()
    cur = await conn.execute(
        """INSERT INTO vps_servers (host, ssh_port, username, password, label, os_info)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (host, ssh_port, username, password, label, os_info),
    )
    await conn.commit()
    return cur.lastrowid


async def get_vps_servers() -> list[dict]:
    """Get all VPS servers."""
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM vps_servers ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def get_vps_server(vps_id: int) -> Optional[dict]:
    """Get a single VPS server by ID."""
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM vps_servers WHERE id = ?", (vps_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_vps_server(vps_id: int, **fields: Any) -> bool:
    """Update VPS server fields."""
    if not fields:
        return False
    conn = await get_db()
    fields["updated_at"] = "datetime('now')"
    sets = []
    vals = []
    for k, v in fields.items():
        if v == "datetime('now')":
            sets.append(f"{k} = datetime('now')")
        else:
            sets.append(f"{k} = ?")
            vals.append(v)
    vals.append(vps_id)
    cur = await conn.execute(
        f"UPDATE vps_servers SET {', '.join(sets)} WHERE id = ?", vals
    )
    await conn.commit()
    return cur.rowcount > 0


async def delete_vps_server(vps_id: int) -> bool:
    """Delete a VPS server."""
    conn = await get_db()
    cur = await conn.execute("DELETE FROM vps_servers WHERE id = ?", (vps_id,))
    await conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# MCP Instances CRUD
# ---------------------------------------------------------------------------

async def add_mcp_instance(workspace_path: str, port: int = 9876) -> int:
    conn = await get_db()
    cur = await conn.execute(
        "INSERT INTO mcp_instances (workspace_path, port) VALUES (?, ?)",
        (workspace_path, port),
    )
    await conn.commit()
    return cur.lastrowid


async def get_mcp_instances() -> list[dict]:
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM mcp_instances ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def get_mcp_instance(mcp_id: int) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM mcp_instances WHERE id = ?", (mcp_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_mcp_instance_by_path(workspace_path: str) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM mcp_instances WHERE workspace_path = ?", (workspace_path,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def update_mcp_instance(mcp_id: int, **fields: Any) -> bool:
    if not fields:
        return False
    conn = await get_db()
    sets = []
    vals = []
    for k, v in fields.items():
        sets.append(f"{k} = ?")
        vals.append(v)
    vals.append(mcp_id)
    cur = await conn.execute(
        f"UPDATE mcp_instances SET {', '.join(sets)} WHERE id = ?", vals
    )
    await conn.commit()
    return cur.rowcount > 0


async def delete_mcp_instance(mcp_id: int) -> bool:
    conn = await get_db()
    cur = await conn.execute("DELETE FROM mcp_instances WHERE id = ?", (mcp_id,))
    await conn.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Chat Sessions + Messages
# ---------------------------------------------------------------------------

async def create_chat_session(
    title: str = "New Chat",
    model: str = "",
    gumloop_account_id: int = 0,
) -> int:
    conn = await get_db()
    cur = await conn.execute(
        "INSERT INTO chat_sessions (title, model, gumloop_account_id) VALUES (?, ?, ?)",
        (title, model, gumloop_account_id),
    )
    await conn.commit()
    return cur.lastrowid


async def get_chat_sessions() -> list[dict]:
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM chat_sessions ORDER BY updated_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def get_chat_session(session_id: int) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute("SELECT * FROM chat_sessions WHERE id = ?", (session_id,))
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_gumloop_session_for_account(account_id: int) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_sessions WHERE gumloop_account_id = ? ORDER BY id ASC LIMIT 1",
        (account_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_chat_session_by_opencode_session_key(opencode_session_key: str) -> Optional[dict]:
    if not opencode_session_key:
        return None
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_sessions WHERE opencode_session_key = ? LIMIT 1",
        (opencode_session_key,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_or_create_chat_session_for_opencode_session(
    opencode_session_key: str,
    title: str = "New Chat",
    model: str = "",
) -> int:
    existing = await get_chat_session_by_opencode_session_key(opencode_session_key)
    if existing:
        return int(existing["id"])

    conn = await get_db()
    try:
        cur = await conn.execute(
            "INSERT INTO chat_sessions (title, model, opencode_session_key) VALUES (?, ?, ?)",
            (title, model, opencode_session_key),
        )
        await conn.commit()
        return cur.lastrowid
    except Exception:
        await conn.rollback()
        existing = await get_chat_session_by_opencode_session_key(opencode_session_key)
        if existing:
            return int(existing["id"])
        raise


async def get_chat_session_by_api_key_id(api_key_id: int) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute(
        """
        SELECT cs.*
        FROM opencode_session_registry r
        JOIN chat_sessions cs ON cs.id = r.chat_session_id
        WHERE r.api_key_id = ?
        LIMIT 1
        """,
        (api_key_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_or_create_chat_session_for_api_key(api_key_id: int, title: str = "New Chat", model: str = "") -> int:
    existing = await get_chat_session_by_api_key_id(api_key_id)
    if existing:
        return int(existing["id"])

    conn = await get_db()
    cur = await conn.execute(
        "INSERT INTO chat_sessions (title, model) VALUES (?, ?)",
        (title, model),
    )
    session_id = cur.lastrowid
    await conn.execute(
        "INSERT INTO opencode_session_registry (api_key_id, chat_session_id, last_model) VALUES (?, ?, ?)",
        (api_key_id, session_id, model),
    )
    await conn.commit()
    return session_id


def _opencode_session_dirs() -> list[Path]:
    candidates = []
    xdg = os.getenv("XDG_CONFIG_HOME", "")
    if xdg:
        candidates.append(Path(xdg) / "opencode" / "sessions")
    candidates.extend([
        Path.home() / ".config" / "opencode" / "sessions",
        Path.home() / ".opencode" / "sessions",
        Path.home() / "AppData" / "Roaming" / "opencode" / "sessions",
    ])
    seen = []
    for p in candidates:
        if p not in seen:
            seen.append(p)
    return seen


def _normalize_path_for_match(path_str: str) -> str:
    try:
        return str(Path(path_str).resolve()).replace("\\", "/").rstrip("/").lower()
    except Exception:
        return str(path_str).replace("\\", "/").rstrip("/").lower()


def _opencode_session_file_candidates(session_key: str) -> list[Path]:
    files = []
    for base in _opencode_session_dirs():
        files.append(base / f"{session_key}.json")
        files.append(base / f"{session_key}.jsonl")
    return files


def _load_json_file(path: Path) -> Optional[dict]:
    try:
        if not path.exists() or not path.is_file():
            return None
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _opencode_message_to_openai(msg: dict) -> dict | None:
    role = str(msg.get("role", "")).strip()
    if role not in ("system", "user", "assistant", "tool"):
        return None
    content = msg.get("content", "")
    if isinstance(content, list):
        text_parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text_parts.append(str(part.get("text", "")))
        content = "\n".join(text_parts)
    elif content is None:
        content = ""
    result = {"role": role, "content": content}
    if role == "tool" and msg.get("tool_call_id"):
        result["tool_call_id"] = msg.get("tool_call_id")
    if role == "assistant" and msg.get("tool_calls"):
        result["tool_calls"] = msg.get("tool_calls")
    return result


def _opencode_transcript_to_openai_messages(payload: dict) -> list[dict]:
    msgs = payload.get("messages", []) if isinstance(payload, dict) else []
    out = []
    for msg in msgs:
        if isinstance(msg, dict):
            m = _opencode_message_to_openai(msg)
            if m:
                out.append(m)
    return out


async def find_opencode_session_key_for_workspace(workspace_dir: str) -> str:
    """Find the most likely OpenCode session ID for a workspace directory."""
    if not workspace_dir:
        return ""
    target = _normalize_path_for_match(workspace_dir)
    best_key = ""
    best_mtime = -1.0

    for base in _opencode_session_dirs():
        if not base.exists():
            continue
        for path in base.glob("*.json"):
            data = _load_json_file(path)
            if not isinstance(data, dict):
                continue
            wd = data.get("working_directory") or data.get("working_dir") or data.get("directory") or ""
            if _normalize_path_for_match(str(wd)) != target:
                continue
            try:
                mtime = path.stat().st_mtime
            except Exception:
                mtime = 0.0
            if mtime > best_mtime:
                best_mtime = mtime
                best_key = str(data.get("id") or path.stem)
    return best_key


async def load_opencode_transcript(session_key: str) -> dict:
    """Load an OpenCode session JSON file and normalize it to a structured transcript."""
    if not session_key:
        return {"session": None, "messages": [], "count": 0}

    payload = None
    for candidate in _opencode_session_file_candidates(session_key):
        payload = _load_json_file(candidate)
        if payload:
            break

    if not payload:
        for base in _opencode_session_dirs():
            if not base.exists():
                continue
            for path in base.glob("*.json"):
                data = _load_json_file(path)
                if isinstance(data, dict) and str(data.get("id", "")) == session_key:
                    payload = data
                    break
            if payload:
                break

    if not isinstance(payload, dict):
        return {"session": None, "messages": [], "count": 0}

    return {
        "session": {
            "id": payload.get("id", session_key),
            "title": payload.get("title", ""),
            "model": payload.get("model", ""),
            "created_at": payload.get("created_at", ""),
            "updated_at": payload.get("updated_at", ""),
            "working_directory": payload.get("working_directory") or payload.get("working_dir") or payload.get("directory", ""),
        },
        "messages": _opencode_transcript_to_openai_messages(payload),
        "count": len(payload.get("messages", []) or []),
    }


async def get_or_create_gumloop_session_for_account(account_id: int, model: str = "gl-claude-sonnet-4-5") -> int:
    """Return the durable persistent chat session for a Gumloop account."""
    existing = await get_gumloop_session_for_account(account_id)
    if existing:
        return existing["id"]

    conn = await get_db()
    title = f"Persistent Session (Account {account_id})"
    try:
        cur = await conn.execute(
            "INSERT INTO chat_sessions (title, model, gumloop_account_id) VALUES (?, ?, ?)",
            (title, model, account_id),
        )
        await conn.commit()
        return cur.lastrowid
    except aiosqlite.IntegrityError:
        existing = await get_gumloop_session_for_account(account_id)
        if existing:
            return existing["id"]
        raise


async def get_or_create_gumloop_interaction_id(session_id: int) -> str:
    """Get existing Gumloop interaction_id for session, or create new one if missing."""
    import uuid
    session = await get_chat_session(session_id)
    if not session:
        return ""

    interaction_id = session.get("gumloop_interaction_id", "")
    if not interaction_id:
        # Generate new interaction_id and save it
        interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
        await update_chat_session(session_id, gumloop_interaction_id=interaction_id)

    return interaction_id


# ---------------------------------------------------------------------------
# Gumloop interaction bindings (per session × account for emulated continuity)
# ---------------------------------------------------------------------------


async def get_gumloop_binding(chat_session_id: int, account_id: int) -> str:
    """Get interaction_id for a (chat_session, account) pair. Returns '' if not found."""
    conn = await get_db()
    cur = await conn.execute(
        "SELECT interaction_id FROM gumloop_interaction_bindings WHERE chat_session_id =? AND account_id =?",
        (chat_session_id, account_id),
    )
    row = await cur.fetchone()
    return row["interaction_id"] if row else ""


async def create_gumloop_binding(chat_session_id: int, account_id: int, interaction_id: str, last_synced_message_id: int | None = None) -> None:
    """Create a (chat_session, account) -> interaction_id binding. Ignores duplicates."""
    conn = await get_db()
    try:
        await conn.execute(
            "INSERT INTO gumloop_interaction_bindings (chat_session_id, account_id, interaction_id, last_synced_message_id) VALUES (?,?,?,?)",
            (chat_session_id, account_id, interaction_id, last_synced_message_id),
        )
        await conn.commit()
    except Exception:
        await conn.execute(
            "UPDATE gumloop_interaction_bindings SET interaction_id =?, updated_at = datetime('now') WHERE chat_session_id =? AND account_id =?",
            (interaction_id, chat_session_id, account_id),
        )
        await conn.commit()


async def get_or_create_gumloop_interaction_for_session_account(
    chat_session_id: int, account_id: int
) -> str:
    """Get or create interaction_id for a (chat_session, account) pair.

    This is the core of emulated continuity:
    - If a binding exists for this (session, account), reuse its interaction_id
    - If not, create a new interaction_id and bind it
    - When account rotates, a new binding is created for the new account,
      but the same logical chat_session continues seamlessly.
    """
    import uuid

    existing = await get_gumloop_binding(chat_session_id, account_id)
    if existing:
        return existing

    interaction_id = str(uuid.uuid4()).replace("-", "")[:22]
    await create_gumloop_binding(chat_session_id, account_id, interaction_id)
    return interaction_id


async def get_all_gumloop_bindings_for_session(chat_session_id: int) -> list[dict]:
    """Get all account bindings for a chat session. Useful for debugging."""
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM gumloop_interaction_bindings WHERE chat_session_id =? ORDER BY created_at ASC",
        (chat_session_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_gumloop_binding_full(chat_session_id: int, account_id: int) -> dict | None:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM gumloop_interaction_bindings WHERE chat_session_id =? AND account_id =?",
        (chat_session_id, account_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_session_messages_after(session_id: int, after_message_id: int) -> list[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? AND id > ? ORDER BY id ASC",
        (session_id, after_message_id),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_latest_session_message_id(session_id: int) -> int:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT MAX(id) as max_id FROM chat_messages WHERE session_id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    return (row["max_id"] or 0) if row else 0


async def update_binding_watermark(chat_session_id: int, account_id: int, last_synced_message_id: int) -> None:
    conn = await get_db()
    await conn.execute(
        "UPDATE gumloop_interaction_bindings SET last_synced_message_id = ?, updated_at = datetime('now') WHERE chat_session_id = ? AND account_id = ?",
        (last_synced_message_id, chat_session_id, account_id),
    )
    await conn.commit()


async def update_chat_session(session_id: int, **fields: Any) -> bool:
    if not fields:
        return False
    conn = await get_db()
    fields["updated_at"] = "datetime('now')"
    sets, vals = [], []
    for k, v in fields.items():
        if v == "datetime('now')":
            sets.append(f"{k} = datetime('now')")
        else:
            sets.append(f"{k} = ?")
            vals.append(v)
    vals.append(session_id)
    cur = await conn.execute(f"UPDATE chat_sessions SET {', '.join(sets)} WHERE id = ?", vals)
    await conn.commit()
    return cur.rowcount > 0


async def delete_chat_session(session_id: int) -> bool:
    conn = await get_db()
    await conn.execute("DELETE FROM chat_messages WHERE session_id = ?", (session_id,))
    cur = await conn.execute("DELETE FROM chat_sessions WHERE id = ?", (session_id,))
    await conn.commit()
    return cur.rowcount > 0


async def add_chat_message(session_id: int, role: str, content: str, model: str = "", opencode_message_id: str = "") -> int:
    conn = await get_db()
    if opencode_message_id:
        cur = await conn.execute(
            "INSERT OR IGNORE INTO chat_messages (session_id, opencode_message_id, role, content, model) VALUES (?, ?, ?, ?, ?)",
            (session_id, opencode_message_id, role, content, model),
        )
        if cur.rowcount == 0:
            existing = await get_chat_message_by_opencode_message_id(session_id, opencode_message_id)
            if existing:
                await conn.execute(
                    "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,),
                )
                await conn.commit()
                return int(existing["id"])
    else:
        cur = await conn.execute(
            "INSERT INTO chat_messages (session_id, opencode_message_id, role, content, model) VALUES (?, ?, ?, ?, ?)",
            (session_id, opencode_message_id, role, content, model),
        )
    await conn.execute(
        "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?", (session_id,),
    )
    await conn.commit()
    return cur.lastrowid


async def get_chat_message_by_opencode_message_id(session_id: int, opencode_message_id: str) -> Optional[dict]:
    if not opencode_message_id:
        return None
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? AND opencode_message_id = ? LIMIT 1",
        (session_id, opencode_message_id),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_chat_messages(session_id: int) -> list[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id ASC", (session_id,),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_last_chat_message(session_id: int) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def get_chat_session_transcript(session_id: int) -> dict:
    """Return a structured JSON transcript for one chat session.

    This is the canonical payload used for Gumloop rehydration/replay.
    """
    session = await get_chat_session(session_id)
    if not session:
        return {"session": None, "messages": [], "count": 0}

    messages = await get_chat_messages(session_id)
    transcript = []
    for idx, msg in enumerate(messages, start=1):
        transcript.append({
            "seq": idx,
            "id": msg.get("id"),
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
            "model": msg.get("model", ""),
            "created_at": msg.get("created_at", ""),
        })

    return {
        "session": {
            "id": session.get("id"),
            "title": session.get("title", ""),
            "model": session.get("model", ""),
            "created_at": session.get("created_at", ""),
            "updated_at": session.get("updated_at", ""),
            "gumloop_account_id": session.get("gumloop_account_id", 0),
            "gumloop_interaction_id": session.get("gumloop_interaction_id", ""),
        },
        "messages": transcript,
        "count": len(transcript),
    }


def _normalize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                elif item.get("type") == "tool_result":
                    parts.append(str(item.get("content", "")))
        return "\n".join(parts).strip()
    return str(content or "").strip()


def _normalize_messages_for_match(messages: list[dict]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    for msg in messages:
        role = str(msg.get("role", "")).strip()
        if role not in ("system", "user", "assistant", "tool", "thinking"):
            continue
        result.append({"role": role, "content": _normalize_message_content(msg.get("content", ""))})
    return result


async def infer_chat_session_id_from_messages(messages: list[dict], model: str = "", opencode_session_key: str = "") -> int:
    """Infer the most likely existing chat session from the incoming message transcript.

    SAFETY: Only matches sessions bound to the same opencode_session_key.
    If no opencode_session_key is provided, returns 0 (refuses to guess broadly).
    Returns 0 if no match is found.
    """
    if not opencode_session_key:
        return 0

    incoming = _normalize_messages_for_match(messages)
    if not incoming:
        return 0

    existing = await get_chat_session_by_opencode_session_key(opencode_session_key)
    if not existing:
        return 0

    sid = int(existing.get("id", 0) or 0)
    if not sid:
        return 0

    transcript = await get_chat_messages(sid)
    stored = _normalize_messages_for_match(transcript)
    if not stored:
        return 0

    if len(stored) > len(incoming):
        return 0

    if incoming[:len(stored)] == stored:
        return sid

    return 0


async def delete_all_chat_sessions() -> int:
    conn = await get_db()
    await conn.execute("DELETE FROM chat_messages")
    cur = await conn.execute("DELETE FROM chat_sessions")
    await conn.commit()
    return cur.rowcount


async def export_chat_data() -> dict:
    """Export all chat sessions + messages as JSON."""
    sessions = await get_chat_sessions()
    result = []
    for s in sessions:
        msgs = await get_chat_messages(s["id"])
        result.append({**s, "messages": msgs})
    return {"sessions": result, "count": len(result)}


async def import_chat_data(data: dict) -> int:
    """Import chat sessions + messages from JSON. Returns count imported."""
    conn = await get_db()
    imported = 0
    for s in data.get("sessions", []):
        cur = await conn.execute(
            "INSERT INTO chat_sessions (title, model, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (s.get("title", "Imported"), s.get("model", ""), s.get("created_at", ""), s.get("updated_at", "")),
        )
        sid = cur.lastrowid
        for m in s.get("messages", []):
            await conn.execute(
                "INSERT INTO chat_messages (session_id, role, content, model, created_at) VALUES (?, ?, ?, ?, ?)",
                (sid, m.get("role", "user"), m.get("content", ""), m.get("model", ""), m.get("created_at", "")),
            )
        imported += 1
    await conn.commit()
    return imported


# ---------------------------------------------------------------------------
# Gumloop session summaries (compacted history for cheaper continuity)
# ---------------------------------------------------------------------------

async def get_session_summary(chat_session_id: int) -> Optional[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM gumloop_session_summaries WHERE chat_session_id = ?",
        (chat_session_id,),
    )
    row = await cur.fetchone()
    return dict(row) if row else None


async def upsert_session_summary(
    chat_session_id: int,
    summary_text: str,
    watermark_message_id: int,
    message_count: int,
) -> None:
    conn = await get_db()
    await conn.execute(
        """INSERT INTO gumloop_session_summaries
           (chat_session_id, summary_text, watermark_message_id, message_count)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(chat_session_id) DO UPDATE SET
             summary_text = excluded.summary_text,
             watermark_message_id = excluded.watermark_message_id,
             message_count = excluded.message_count,
             updated_at = datetime('now')""",
        (chat_session_id, summary_text, watermark_message_id, message_count),
    )
    await conn.commit()


async def get_session_messages_between(
    session_id: int, after_id: int, limit: int = 50
) -> list[dict]:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT * FROM chat_messages WHERE session_id = ? AND id > ? ORDER BY id ASC LIMIT ?",
        (session_id, after_id, limit),
    )
    return [dict(r) for r in await cur.fetchall()]


async def get_session_message_count(session_id: int) -> int:
    conn = await get_db()
    cur = await conn.execute(
        "SELECT COUNT(*) as cnt FROM chat_messages WHERE session_id = ?",
        (session_id,),
    )
    row = await cur.fetchone()
    return row["cnt"] if row else 0


async def get_last_gumloop_account_id(chat_session_id: int) -> int:
    session = await get_chat_session(chat_session_id)
    if not session:
        return 0
    try:
        return int(session.get("last_gumloop_account_id", 0) or 0)
    except Exception:
        return 0
