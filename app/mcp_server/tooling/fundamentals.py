"""Fundamentals tool registration for MCP."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.registrars import register_tool_subset

if TYPE_CHECKING:
    from fastmcp import FastMCP

FUNDAMENTALS_TOOL_NAMES: set[str] = {
    "get_news",
    "get_company_profile",
    "get_crypto_profile",
    "get_financials",
    "get_insider_transactions",
    "get_earnings_calendar",
    "get_investor_trends",
    "get_investment_opinions",
    "get_valuation",
    "get_short_interest",
    "get_kimchi_premium",
    "get_funding_rate",
    "get_market_index",
    "get_support_resistance",
    "get_sector_peers",
}


def register_fundamentals_tools(mcp: FastMCP) -> None:
    register_tool_subset(mcp, FUNDAMENTALS_TOOL_NAMES)


__all__ = ["FUNDAMENTALS_TOOL_NAMES", "register_fundamentals_tools"]
