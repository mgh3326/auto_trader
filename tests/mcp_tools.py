"""Test-time compatibility helper for MCP tool module access.

This shim keeps test code stable while removing legacy public imports from the
application package itself. Tests import ``mcp_tools`` from this module and use it
as a mutable namespace for patching tool implementation symbols.
"""

from __future__ import annotations

from typing import Any

import httpx
import yfinance as yf

import app.mcp_server.tooling.analysis_rankings as analysis_rankings
import app.mcp_server.tooling.analysis_recommend as analysis_recommend
import app.mcp_server.tooling.analysis_screen_core as analysis_screen_core
import app.mcp_server.tooling.analysis_screening as analysis_screening
import app.mcp_server.tooling.analysis_tool_handlers as analysis_tool_handlers
import app.mcp_server.tooling.fundamentals as fundamentals
import app.mcp_server.tooling.fundamentals_sources as fundamentals_sources
import app.mcp_server.tooling.market_data as market_data
import app.mcp_server.tooling.order_execution as order_execution
import app.mcp_server.tooling.orders as orders
import app.mcp_server.tooling.portfolio as portfolio
import app.mcp_server.tooling.shared as shared
import app.services.naver_finance as naver_finance
import app.services.upbit as upbit_service
import app.services.yahoo as yahoo_service
from app.core.config import settings
from app.mcp_server.tooling.registry import register_all_tools
from app.services.kis import KISClient

_TEST_SHIM_TARGETS = (
    settings,
    order_execution,
    upbit_service,
    naver_finance,
    yahoo_service,
    KISClient,
    httpx,
    yf,
)

_PROXY_MODULES = (
    analysis_rankings,
    analysis_recommend,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals,
    fundamentals_sources,
    market_data,
    order_execution,
    orders,
    portfolio,
    shared,
)


def _get_attr_from_modules(name: str) -> Any:
    for module in _PROXY_MODULES:
        if hasattr(module, name):
            return getattr(module, name)
    raise AttributeError(name)


def _set_attr_on_modules(name: str, value: Any) -> None:
    for module in _PROXY_MODULES:
        if hasattr(module, name):
            setattr(module, name, value)
    if name in {
        "settings",
        "upbit_service",
        "naver_finance",
        "yahoo_service",
        "yf",
        "httpx",
        "KISClient",
    }:
        globals()[name] = value


class _MCPToolsNamespace:
    def __getattr__(self, name: str) -> Any:
        if name in {
            "settings",
            "upbit_service",
            "naver_finance",
            "yahoo_service",
            "yf",
            "httpx",
            "KISClient",
        }:
            return globals()[name]
        if name in {"register_all_tools", "register_tools"}:
            return register_all_tools
        return _get_attr_from_modules(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {
            "settings",
            "upbit_service",
            "naver_finance",
            "yahoo_service",
            "yf",
            "httpx",
            "KISClient",
        }:
            if name == "KISClient":
                globals()["KISClient"] = value
            else:
                globals()[name] = value
            _set_attr_on_modules(name, value)
            return
        if name in {"register_all_tools", "register_tools"}:
            return
        _set_attr_on_modules(name, value)
        globals()[name] = value

    def __dir__(self) -> list[str]:
        names: set[str] = {
            "settings",
            "upbit_service",
            "naver_finance",
            "yahoo_service",
            "yf",
            "httpx",
            "KISClient",
            "register_all_tools",
            "register_tools",
            "_get_dca_status_impl",
        }
        for module in _PROXY_MODULES:
            names.update(dir(module))
        return sorted(names)


mcp_tools = _MCPToolsNamespace()

__all__ = ["mcp_tools"]
