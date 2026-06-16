"""Portfolio MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.portfolio_allocation import (
    ALLOCATION_TOOL_NAMES,
    register_portfolio_allocation_tool,
)
from app.mcp_server.tooling.portfolio_holdings import (
    PORTFOLIO_TOOL_NAMES as HOLDINGS_TOOL_NAMES,
)
from app.mcp_server.tooling.portfolio_holdings import (
    _register_portfolio_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

PORTFOLIO_TOOL_NAMES: set[str] = HOLDINGS_TOOL_NAMES | ALLOCATION_TOOL_NAMES


def register_portfolio_tools(mcp: FastMCP) -> None:
    _register_portfolio_tools_impl(mcp)
    register_portfolio_allocation_tool(mcp)


__all__ = ["PORTFOLIO_TOOL_NAMES", "register_portfolio_tools"]
