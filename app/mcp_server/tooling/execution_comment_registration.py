"""MCP registration for execution comment tools."""

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
            "Format a structured Markdown comment for trade execution events. "
            "stage='fill' for immediate fill notification with price/qty/thesis. "
            "stage='follow_up' for post-fill analysis with next_action recommendation. "
            "Output is usable in both Discord and Paperclip comments."
        ),
    )(format_execution_comment)


__all__ = [
    "EXECUTION_COMMENT_TOOL_NAMES",
    "register_execution_comment_tools",
]
