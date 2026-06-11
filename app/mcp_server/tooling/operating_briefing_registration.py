"""MCP registration for ROB-517 operating briefing tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.operating_briefing import (
    get_operating_briefing_impl,
    list_active_watches_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


OPERATING_BRIEFING_TOOL_NAMES: set[str] = {
    "list_active_watches",
    "get_operating_briefing",
}


def register_operating_briefing_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="list_active_watches",
        description=(
            "Read-only: list actionable investment_watch_alerts rows with "
            "status='active'. Defaults to rows whose valid_until is still in "
            "the future; pass include_expired_status_rows=True for diagnostics."
        ),
    )(list_active_watches_impl)
    mcp.tool(
        name="get_operating_briefing",
        description=(
            "Read-only: one-call session bootstrap for current operating state. "
            "Returns holdings summary, pending orders, active watches, latest "
            "advisory report summary, recent session context, and per-section "
            "staleness metadata. No broker/order/watch/session mutation."
        ),
    )(get_operating_briefing_impl)


__all__ = [
    "OPERATING_BRIEFING_TOOL_NAMES",
    "register_operating_briefing_tools",
]
