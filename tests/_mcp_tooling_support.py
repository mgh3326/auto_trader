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
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server.tooling import (
    analysis_analyze,
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
from app.mcp_server.tooling.screening import crypto as screening_crypto
from app.mcp_server.tooling.screening import kr as screening_kr
from app.mcp_server.tooling.screening import us as screening_us


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


class _TvCondition:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return isinstance(other, _TvCondition) and self.label == other.label

    def __and__(self, other: object) -> object:
        raise AssertionError("crypto filters must not be combined with '&'")


class _TvField:
    def __init__(self, label: str, name: str | None = None) -> None:
        self.label = label
        self.name = name or label.upper()

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return cast(bool, cast(object, _TvCondition(f"{self.label}=={other}")))

    def isin(self, other: object) -> _TvCondition:
        values = list(cast(Any, other))
        return _TvCondition(f"{self.label} in {values}")


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
    analysis_analyze,
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
    screening_kr,
    screening_us,
    screening_crypto,
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


@pytest.fixture
def fake_crypto_tvscreener_module() -> SimpleNamespace:
    return SimpleNamespace(
        CryptoField=SimpleNamespace(
            NAME=_TvField("name", "NAME"),
            DESCRIPTION=_TvField("description", "DESCRIPTION"),
            PRICE=_TvField("price", "PRICE"),
            CHANGE_PERCENT=_TvField("change_percent", "CHANGE_PERCENT"),
            RELATIVE_STRENGTH_INDEX_14=_TvField("rsi14", "RELATIVE_STRENGTH_INDEX_14"),
            AVERAGE_DIRECTIONAL_INDEX_14=_TvField(
                "adx14", "AVERAGE_DIRECTIONAL_INDEX_14"
            ),
            VOLUME_24H_IN_USD=_TvField("volume24h", "VOLUME_24H_IN_USD"),
            VALUE_TRADED=_TvField("value_traded", "VALUE_TRADED"),
            MARKET_CAP=_TvField("market_cap", "MARKET_CAP"),
            EXCHANGE=_TvField("exchange", "EXCHANGE"),
        )
    )


@pytest.fixture
def mock_krx_stocks() -> list[dict[str, Any]]:
    return [
        {
            "code": "005930",
            "name": "삼성전자",
            "close": 80000.0,
            "change_rate": 2.5,
            "change_price": 2000,
            "market": "KOSPI",
            "market_cap": 4800000,
        },
        {
            "code": "000660",
            "name": "SK하이닉스",
            "close": 150000.0,
            "change_rate": -1.2,
            "change_price": -1800,
            "market": "KOSPI",
            "market_cap": 150000,
        },
    ]


@pytest.fixture
def mock_krx_etfs() -> list[dict[str, Any]]:
    return [
        {
            "code": "069500",
            "name": "KODEX 200",
            "close": 45000.0,
            "market": "KOSPI",
            "market_cap": 45000,
            "index_name": "KOSPI 200",
        },
        {
            "code": "114800",
            "name": "KODEX 반도체",
            "close": 12000.0,
            "market": "KOSPI",
            "market_cap": 1200,
            "index_name": "Wise 반도체지수",
        },
    ]


@pytest.fixture
def mock_valuation_data() -> dict[str, dict[str, Any]]:
    return {
        "005930": {"per": 12.5, "pbr": 1.2, "dividend_yield": 0.0256},
        "000660": {"per": None, "pbr": None, "dividend_yield": None},
        "035420": {"per": 0, "pbr": 0.8, "dividend_yield": 0.035},
    }


@pytest.fixture
def mock_yfinance_screen() -> Callable[..., dict[str, Any]]:
    def mock_screen_func(query, size, sortField, sortAsc, session=None):
        assert session is not None
        return {
            "quotes": [
                {
                    "symbol": "AAPL",
                    "shortname": "Apple Inc.",
                    "lastprice": 175.5,
                    "percentchange": 1.2,
                    "dayvolume": 50000000,
                    "intradaymarketcap": 2800000000000,
                    "peratio": 28.5,
                    "forward_dividend_yield": 0.005,
                },
                {
                    "symbol": "MSFT",
                    "shortname": "Microsoft Corp",
                    "lastprice": 330.0,
                    "percentchange": -0.5,
                    "dayvolume": 20000000,
                    "intradaymarketcap": 2500000000000,
                    "peratio": 32.0,
                    "forward_dividend_yield": 0.008,
                },
                {
                    "symbol": "GOOGL",
                    "shortname": "Alphabet Inc.",
                    "lastprice": 140.0,
                    "percentchange": 0.8,
                    "dayvolume": 15000000,
                    "intradaymarketcap": 1500000000000,
                    "peratio": 22.0,
                    "forward_dividend_yield": 0.0,
                },
            ]
        }

    return mock_screen_func


@pytest.fixture
def mock_upbit_coins() -> list[dict[str, Any]]:
    return [
        {
            "market": "KRW-BTC",
            "korean_name": "비트코인",
            "trade_price": 100_000_000,
            "signed_change_rate": 0.01,
            "acc_trade_price_24h": 1_000_000_000_000,
        },
        {
            "market": "KRW-ETH",
            "korean_name": "이더리움",
            "trade_price": 5_000_000,
            "signed_change_rate": 0.02,
            "acc_trade_price_24h": 800_000_000_000,
        },
    ]


@pytest.fixture(autouse=True)
def _mock_crypto_external_sources(monkeypatch: pytest.MonkeyPatch):
    async def mock_get_upbit_warning_markets(
        db=None,
        quote_currency: str | None = None,
        fiat: str | None = None,
    ):
        _ = (quote_currency, fiat, db)
        return set()

    async def mock_market_cap_cache_get():
        return {
            "data": {},
            "cached": True,
            "age_seconds": 0.0,
            "stale": False,
            "error": None,
        }

    async def mock_fetch_ohlcv_for_indicators(
        symbol: str, market_type: str, count: int
    ):
        del symbol, market_type, count
        return pd.DataFrame()

    monkeypatch.setattr(
        screening_crypto,
        "get_upbit_warning_markets",
        mock_get_upbit_warning_markets,
    )
    monkeypatch.setattr(
        screening_crypto._CRYPTO_MARKET_CAP_CACHE,
        "get",
        mock_market_cap_cache_get,
    )
    monkeypatch.setattr(
        screening_crypto,
        "_fetch_ohlcv_for_indicators",
        mock_fetch_ohlcv_for_indicators,
    )
    monkeypatch.setattr(
        screening_kr,
        "_fetch_ohlcv_for_indicators",
        mock_fetch_ohlcv_for_indicators,
    )
    monkeypatch.setattr(
        screening_us,
        "_fetch_ohlcv_for_indicators",
        mock_fetch_ohlcv_for_indicators,
    )

    # Mock tvscreener globally for TvScreenerService to avoid live calls in tests
    # and ensure consistent behavior with mocked data.
    from app.services import tvscreener_service

    # We can't easily import fake_crypto_tvscreener_module here as it's a fixture,
    # but we can define a simple mock or use the one from screening_crypto if already patched.
    # For now, let's just make it raise ImportError if not explicitly mocked in the test,
    # which will trigger legacy fallback in most MCP tests.
    def mock_import_tvscreener():
        raise ImportError(
            "tvscreener disabled by default in tests to prevent live calls"
        )

    monkeypatch.setattr(
        tvscreener_service,
        "_import_tvscreener",
        mock_import_tvscreener,
    )


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
