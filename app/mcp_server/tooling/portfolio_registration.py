"""Portfolio MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.portfolio_holdings import (
    PORTFOLIO_TOOL_NAMES,
    _register_portfolio_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_portfolio_tools(mcp: FastMCP) -> None:
    _register_portfolio_tools_impl(mcp)


__all__ = ["PORTFOLIO_TOOL_NAMES", "register_portfolio_tools"]
