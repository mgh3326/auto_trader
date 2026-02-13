"""Portfolio tool registration for MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.registrars import register_tool_subset

if TYPE_CHECKING:
    from fastmcp import FastMCP

PORTFOLIO_TOOL_NAMES: set[str] = {
    "get_holdings",
    "get_position",
    "get_cash_balance",
    "simulate_avg_cost",
    "update_manual_holdings",
    "create_dca_plan",
    "get_dca_status",
}


def register_portfolio_tools(mcp: FastMCP) -> None:
    register_tool_subset(mcp, PORTFOLIO_TOOL_NAMES)


__all__ = ["PORTFOLIO_TOOL_NAMES", "register_portfolio_tools"]
