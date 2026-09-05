"""MCP registration for market brief and reports tools."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.market_brief_tools import (
    get_latest_market_brief,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

MARKET_BRIEF_TOOL_NAMES: set[str] = {"get_latest_market_brief"}


def register_market_brief_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="get_latest_market_brief",
        description=(
            "Get a concise market summary from recent AI analysis results. "
            "Returns decision (buy/hold/sell), confidence, and key price levels "
            "for each symbol. Use for quick market context during trade execution."
        ),
    )(get_latest_market_brief)


__all__ = [
    "MARKET_BRIEF_TOOL_NAMES",
    "register_market_brief_tools",
]
