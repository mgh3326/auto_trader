"""MCP registration for Paperclip comment posting tool."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.paperclip_comment import post_paperclip_comment

if TYPE_CHECKING:
    from fastmcp import FastMCP

PAPERCLIP_COMMENT_TOOL_NAMES: set[str] = {
    "post_paperclip_comment",
}


def register_paperclip_comment_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="post_paperclip_comment",
        description=(
            "Post a markdown comment to a Paperclip issue. "
            "Requires PAPERCLIP_API_URL and PAPERCLIP_API_KEY environment variables. "
            "Pass issue_identifier (e.g. 'ROB-73') and body (markdown text)."
        ),
    )(post_paperclip_comment)


__all__ = [
    "PAPERCLIP_COMMENT_TOOL_NAMES",
    "register_paperclip_comment_tools",
]
