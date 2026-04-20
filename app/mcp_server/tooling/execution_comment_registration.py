"""MCP registration for execution comment formatter tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.execution_comment_tools import format_execution_comment

if TYPE_CHECKING:
    from fastmcp import FastMCP

EXECUTION_COMMENT_TOOL_NAMES: set[str] = {
    "format_execution_comment",
}


def register_execution_comment_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="format_execution_comment",
        description=(
            "Format trade execution data into a structured markdown comment. "
            "stage controls which fields appear: "
            "'strategy' (symbol, side, thesis), "
            "'dry_run' (symbol, side, qty, price), "
            "'live' (all fields, fill_status=pending), "
            "'fill' (all fields incl. filled_qty/fee), "
            "'follow_up' (symbol, journal_id, next_action, market_context). "
            "Missing optional fields are omitted. "
            "Set currency ('$', '₩') to prepend to price/fee."
        ),
    )(format_execution_comment)


__all__ = [
    "EXECUTION_COMMENT_TOOL_NAMES",
    "register_execution_comment_tools",
]
