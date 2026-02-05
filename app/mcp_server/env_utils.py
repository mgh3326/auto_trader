"""Environment variable utilities for MCP server."""

from __future__ import annotations

import logging
import os


def _env(name: str, default: str | None = None) -> str | None:
    """Get environment variable, returning default if not set or empty."""
    v = os.getenv(name)
    return v if v not in (None, "") else default


def _env_int(name: str, default: int) -> int:
    """Get environment variable as int, returning default if invalid."""
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        logging.warning(f"Invalid integer for {name}={raw!r}, using default={default}")
        return default
