"""MCP registration for market brief and reports tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.market_brief_tools import (
    get_latest_market_brief,
    get_market_reports,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

MARKET_BRIEF_TOOL_NAMES: set[str] = {
    "get_latest_market_brief",
    "get_market_reports",
}


def register_market_brief_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_latest_market_brief",
        description=(
            "Get a concise market summary from recent AI analysis results. "
            "Returns decision (buy/hold/sell), confidence, and key price levels "
            "for each symbol. Use for quick market context during trade execution."
        ),
    )(get_latest_market_brief)
    _ = mcp.tool(
        name="get_market_reports",
        description=(
            "Get detailed analysis report history for a specific symbol. "
            "Returns full analysis including reasons, price ranges, detailed text, "
            "and decision trend over time. Use for deep-dive on a single symbol."
        ),
    )(get_market_reports)


__all__ = [
    "MARKET_BRIEF_TOOL_NAMES",
    "register_market_brief_tools",
]
