"""Fundamentals MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.fundamentals_handlers import (
    FUNDAMENTALS_TOOL_NAMES,
    _register_fundamentals_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_fundamentals_tools(mcp: FastMCP) -> None:
    _register_fundamentals_tools_impl(mcp)


__all__ = ["FUNDAMENTALS_TOOL_NAMES", "register_fundamentals_tools"]
