"""Market-data MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.market_data_quotes import (
    MARKET_DATA_TOOL_NAMES,
    _register_market_data_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_market_data_tools(mcp: FastMCP) -> None:
    _register_market_data_tools_impl(mcp)


__all__ = ["MARKET_DATA_TOOL_NAMES", "register_market_data_tools"]
