"""Legacy compatibility facade for MCP tools.

Historically, this module contained a monolithic ``register_tools`` function with
many nested implementations. The real implementations now live in domain modules
under ``app.mcp_server.tooling`` and are registered via ``register_all_tools``.

This file remains as a backward-compatible shim for:
- imports of ``app.mcp_server.tooling.legacy_tools``
- old call sites expecting ``register_tools`` symbol
- monkeypatch/test patch points resolved through module attributes
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yfinance as yf

from app.core.config import settings
from app.mcp_server.tooling import (
    analysis_screening as _analysis_screening,
)
from app.mcp_server.tooling import (
    fundamentals as _fundamentals,
)
from app.mcp_server.tooling import (
    fundamentals_sources as _fundamentals_sources,
)
from app.mcp_server.tooling import (
    market_data as _market_data,
)
from app.mcp_server.tooling import (
    orders as _orders,
)
from app.mcp_server.tooling import (
    portfolio as _portfolio,
)
from app.mcp_server.tooling import (
    shared as _shared,
)
from app.mcp_server.tooling.registry import register_all_tools as _register_all_tools
from app.services import naver_finance
from app.services import upbit as upbit_service
from app.services import yahoo as yahoo_service
from app.services.kis import KISClient

try:
    from app.services.disclosures.dart import list_filings
except ImportError:
    list_filings = None

if TYPE_CHECKING:
    from fastmcp import FastMCP

register_all_tools = _register_all_tools

_MODULE_SEARCH_ORDER = (
    _orders,
    _portfolio,
    _analysis_screening,
    _fundamentals,
    _market_data,
    _shared,
    _fundamentals_sources,
)


def register_tools(mcp: FastMCP) -> None:
    """Legacy alias for the refactored domain-based registration entrypoint."""
    register_all_tools(mcp)


def __getattr__(name: str) -> Any:
    for module in _MODULE_SEARCH_ORDER:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(f"module '{__name__}' has no attribute '{name}'")


def __dir__() -> list[str]:
    names = set(globals().keys())
    for module in _MODULE_SEARCH_ORDER:
        names.update(dir(module))
    return sorted(names)


__all__ = [
    "register_tools",
    "register_all_tools",
    "KISClient",
    "upbit_service",
    "yahoo_service",
    "naver_finance",
    "settings",
    "yf",
    "list_filings",
]
