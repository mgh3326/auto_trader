"""Request-scoped MCP caller identity helpers."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Literal

CallerSource = Literal["http_header", "env_fallback", "none"]

caller_agent_id_var: ContextVar[str | None] = ContextVar(
    "mcp_caller_agent_id",
    default=None,
)
caller_source_var: ContextVar[CallerSource] = ContextVar(
    "mcp_caller_source",
    default="none",
)


def get_caller_agent_id() -> str | None:
    """Return the current MCP caller agent id, if one was provided."""
    return caller_agent_id_var.get()


def get_caller_source() -> CallerSource:
    """Return the source used to resolve the current MCP caller identity."""
    return caller_source_var.get()
