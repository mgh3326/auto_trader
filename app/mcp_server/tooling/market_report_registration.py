from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.market_report_handlers import (
    MARKET_REPORT_TOOL_NAMES,
    _register_market_report_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_market_report_tools(mcp: FastMCP) -> None:
    _register_market_report_tools_impl(mcp)


__all__ = ["MARKET_REPORT_TOOL_NAMES", "register_market_report_tools"]
