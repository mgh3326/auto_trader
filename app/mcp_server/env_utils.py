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


def get_finnhub_api_key() -> str | None:
    """Get Finnhub API key from environment."""
    return _env("FINNHUB_API_KEY")


def get_mcp_graceful_shutdown_timeout() -> int:
    """Get MCP HTTP graceful shutdown timeout in seconds."""
    return _env_int("MCP_GRACEFUL_SHUTDOWN_TIMEOUT", 10)


# Single source of truth for the per-tool timeout default (imported by
# app.mcp_server.timeout_middleware so the two never drift).
DEFAULT_MCP_TOOL_TIMEOUT_S = 45.0


def get_mcp_tool_timeout_default() -> float:
    """Default per-tool execution timeout (seconds) for the MCP timeout middleware."""
    raw = _env("MCP_TOOL_TIMEOUT_DEFAULT_S")
    if raw is None:
        return DEFAULT_MCP_TOOL_TIMEOUT_S
    try:
        return float(raw)
    except ValueError:
        logging.warning(
            f"Invalid float for MCP_TOOL_TIMEOUT_DEFAULT_S={raw!r}, "
            f"using default={DEFAULT_MCP_TOOL_TIMEOUT_S}"
        )
        return DEFAULT_MCP_TOOL_TIMEOUT_S


def get_mcp_tool_timeout_enabled() -> bool:
    """Kill switch for the MCP per-tool timeout middleware (default enabled)."""
    raw = _env("MCP_TOOL_TIMEOUT_ENABLED")
    if raw is None:
        return True
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_mcp_heartbeat_path() -> str | None:
    """Path the MCP server writes its liveness heartbeat to. None disables it
    (the watchdog only runs in the native deployment, which sets this)."""
    return _env("MCP_HEARTBEAT_PATH")


def get_mcp_heartbeat_interval_s() -> float:
    """Seconds between MCP heartbeat writes (default 10)."""
    raw = _env("MCP_HEARTBEAT_INTERVAL_S")
    if raw is None:
        return 10.0
    try:
        return float(raw)
    except ValueError:
        logging.warning(
            f"Invalid float for MCP_HEARTBEAT_INTERVAL_S={raw!r}, using default=10.0"
        )
        return 10.0


def get_mcp_color() -> str:
    """Deployment color (blue/green) for heartbeat tagging; 'unknown' if unset."""
    return _env("AUTO_TRADER_COLOR") or "unknown"
