"""Orders MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.orders_history import (
    ORDER_TOOL_NAMES,
    _register_order_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_order_tools(mcp: FastMCP) -> None:
    _register_order_tools_impl(mcp)


__all__ = ["ORDER_TOOL_NAMES", "register_order_tools"]
