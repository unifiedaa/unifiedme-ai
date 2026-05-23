from __future__ import annotations

import os
from pathlib import Path


ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_local_env() -> Path:
    """Load simple KEY=VALUE pairs from repo-root .env into os.environ.

    Existing environment variables win.
    Supports comments (# ...) and optional single/double quotes.
    """
    if not ENV_PATH.exists():
        return ENV_PATH

    for raw_line in ENV_PATH.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value
    return ENV_PATH
