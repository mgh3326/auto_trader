"""
Shared scaffolding utilities for MCP tooling tests.

This module provides reusable test utilities for testing MCP server tools.
Import from this module rather than duplicating code across test files.

Usage:
    from tests._mcp_tooling_support import (
        DummyMCP,
        DummySessionManager,
        build_tools,
        _patch_runtime_attr,
    )
"""

from collections.abc import Callable
from typing import Any, cast

import pandas as pd
import pytest
from unittest.mock import AsyncMock

from app.mcp_server.tooling import (
    analysis_rankings,
    analysis_recommend,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals_handlers,
    fundamentals_sources_binance,
    fundamentals_sources_coingecko,
    fundamentals_sources_finnhub,
    fundamentals_sources_indices,
    fundamentals_sources_naver,
    market_data_indicators,
    market_data_quotes,
    order_execution,
    orders_history,
    orders_modify_cancel,
    portfolio_cash,
    portfolio_holdings,
)
from app.mcp_server.tooling.registry import register_all_tools


class DummyMCP:
    """
    Mock MCP server that captures tool registrations for testing.

    Use with build_tools() to get a dict of all registered tools.
    """

    def __init__(self) -> None:
        self.tools: dict[str, Callable[..., Any]] = {}

    def tool(self, name: str, description: str):
        """Decorator that captures tool registrations."""
        del description

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self.tools[name] = func
            return func

        return decorator


class DummySessionManager:
    """Async context manager wrapper for an AsyncSession-like object."""

    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self.db

    async def __aexit__(self, exc_type, exc, tb):
        return None


class _ScalarResult:
    """Helper class for mocking database scalar query results."""

    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _DummyRouteDB:
    """
    Dummy database for route testing that returns pre-configured results.

    Each call to execute() returns the next value from execute_results.
    """

    def __init__(self, execute_results: list[object | None]):
        self._execute_results = list(execute_results)
        self.calls = 0

    async def execute(self, query):
        del query
        if self.calls < len(self._execute_results):
            value = self._execute_results[self.calls]
        else:
            value = None
        self.calls += 1
        return _ScalarResult(value)


# Modules that expose runtime attributes that may need patching
_PATCH_MODULES = (
    analysis_rankings,
    analysis_recommend,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals_handlers,
    fundamentals_sources_binance,
    fundamentals_sources_coingecko,
    fundamentals_sources_finnhub,
    fundamentals_sources_indices,
    fundamentals_sources_naver,
    market_data_indicators,
    market_data_quotes,
    order_execution,
    orders_history,
    orders_modify_cancel,
    portfolio_cash,
    portfolio_holdings,
)


def build_tools() -> dict[str, Callable[..., Any]]:
    """
    Build and return a dict of all registered MCP tools.

    Returns:
        Dictionary mapping tool names to their handler functions.
    """
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp))
    return mcp.tools


def _patch_runtime_attr(
    monkeypatch: pytest.MonkeyPatch, attr_name: str, value: object
) -> None:
    """
    Patch a runtime attribute across all modules that expose it.

    Args:
        monkeypatch: pytest monkeypatch fixture
        attr_name: Name of the attribute to patch
        value: New value for the attribute

    Raises:
        AttributeError: If no module exposes the specified attribute
    """
    matched = False
    for module in _PATCH_MODULES:
        if hasattr(module, attr_name):
            monkeypatch.setattr(module, attr_name, value)
            matched = True
    if not matched:
        raise AttributeError(f"No runtime module exposes attribute '{attr_name}'")


def _upbit_name_lookup_mock(name_map: dict[str, str]) -> AsyncMock:
    """
    Create a mock for Upbit symbol name lookup.

    Args:
        name_map: Dictionary mapping currency symbols to display names

    Returns:
        AsyncMock that implements the lookup function
    """
    async def _lookup(currency: str, quote_currency: str = "KRW", db=None) -> str:
        _ = quote_currency, db
        key = str(currency).upper()
        return name_map.get(key, key)

    return AsyncMock(side_effect=_lookup)


def _patch_httpx_async_client(
    monkeypatch: pytest.MonkeyPatch, async_client_class: type
) -> None:
    """
    Patch httpx.AsyncClient across all modules that use it.

    Args:
        monkeypatch: pytest monkeypatch fixture
        async_client_class: Mock class to replace AsyncClient
    """
    for module in (
        analysis_tool_handlers,
        fundamentals_sources_binance,
        fundamentals_sources_coingecko,
        fundamentals_sources_indices,
        fundamentals_sources_naver,
    ):
        monkeypatch.setattr(module.httpx, "AsyncClient", async_client_class)


def _patch_yf_ticker(
    monkeypatch: pytest.MonkeyPatch,
    ticker_factory: Callable[[str], object],
) -> None:
    """
    Patch yfinance.Ticker across modules that use it.

    Args:
        monkeypatch: pytest monkeypatch fixture
        ticker_factory: Factory function that creates Ticker mock objects
    """
    def wrapped_ticker(symbol, session=None):
        assert session is not None
        return ticker_factory(symbol)

    monkeypatch.setattr(fundamentals_sources_naver.yf, "Ticker", wrapped_ticker)
    monkeypatch.setattr(fundamentals_sources_indices.yf, "Ticker", wrapped_ticker)


def _single_row_df() -> pd.DataFrame:
    """
    Create a single-row DataFrame with standard OHLCV columns.

    Useful for mocking price/volume data in tests.

    Returns:
        DataFrame with one row of sample data
    """
    return pd.DataFrame(
        [
            {
                "date": "2024-01-01",
                "time": "09:30:00",
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
            }
        ]
    )


# Sync hint for KR symbol universe
_KR_SYNC_HINT = "uv run python scripts/sync_kr_symbol_universe.py"
