"""Tool registration orchestration for MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.analysis_registration import register_analysis_tools
from app.mcp_server.tooling.fundamentals import register_fundamentals_tools
from app.mcp_server.tooling.market_data import register_market_data_tools
from app.mcp_server.tooling.orders import register_order_tools
from app.mcp_server.tooling.portfolio import register_portfolio_tools

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_all_tools(mcp: FastMCP) -> None:
    register_market_data_tools(mcp)
    register_portfolio_tools(mcp)
    register_order_tools(mcp)
    register_fundamentals_tools(mcp)
    register_analysis_tools(mcp)


__all__ = ["register_all_tools"]
