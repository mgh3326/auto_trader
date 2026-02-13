"""Order tool registration for MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.registrars import register_tool_subset

if TYPE_CHECKING:
    from fastmcp import FastMCP

ORDER_TOOL_NAMES: set[str] = {
    "place_order",
    "modify_order",
    "cancel_order",
    "get_order_history",
}


def register_order_tools(mcp: FastMCP) -> None:
    register_tool_subset(mcp, ORDER_TOOL_NAMES)


__all__ = ["ORDER_TOOL_NAMES", "register_order_tools"]
