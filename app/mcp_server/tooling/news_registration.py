from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.news_handlers import (
    NEWS_TOOL_NAMES,
    _register_news_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_news_tools(mcp: FastMCP) -> None:
    _register_news_tools_impl(mcp)


__all__ = ["NEWS_TOOL_NAMES", "register_news_tools"]
