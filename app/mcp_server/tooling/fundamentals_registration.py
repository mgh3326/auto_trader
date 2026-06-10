"""Fundamentals MCP tool registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.mcp_server.tooling.fundamentals_handlers import (
    CRYPTO_FUNDAMENTALS_TOOL_NAMES,
    FUNDAMENTALS_TOOL_NAMES,
    _register_fundamentals_tools_impl,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


def register_fundamentals_tools(
    mcp: FastMCP,
    *,
    include_crypto: bool = True,
) -> None:
    _register_fundamentals_tools_impl(mcp, include_crypto=include_crypto)


__all__ = [
    "CRYPTO_FUNDAMENTALS_TOOL_NAMES",
    "FUNDAMENTALS_TOOL_NAMES",
    "register_fundamentals_tools",
]
