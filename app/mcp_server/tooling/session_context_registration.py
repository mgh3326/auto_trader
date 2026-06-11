"""MCP registration for ROB-516 session context tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.session_context_tools import (
    session_context_append,
    session_context_get_recent,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


SESSION_CONTEXT_TOOL_NAMES: set[str] = {
    "session_context_append",
    "session_context_get_recent",
}


def register_session_context_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="session_context_append",
        description=(
            "Append operator session context entries for cross-session handoff. "
            "Use for plans, decisions, deferred items, rejected candidates, "
            "constraints, open questions, next actions, and handoff notes. "
            "This is append-only operational context, not an investment report."
        ),
    )(session_context_append)
    _ = mcp.tool(
        name="session_context_get_recent",
        description=(
            "Read recent operator session context entries, newest first. "
            "Optional filters: market, account_scope, kst_date_from, entry_type, "
            "limit clamped to 1..100. Call this at new-session startup before "
            "running the next trading tournament."
        ),
    )(session_context_get_recent)


__all__ = [
    "SESSION_CONTEXT_TOOL_NAMES",
    "register_session_context_tools",
]
