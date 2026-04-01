# pyright: reportMissingImports=false
"""Tests for screen_stocks MCP tool."""

import logging
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
from app.core.async_rate_limiter import RateLimitExceededError
from app.mcp_server.tooling import (
    analysis_screen_core,
    analysis_screening,
    fundamentals_sources_naver,
)
from app.mcp_server.tooling.registry import register_all_tools
from app.mcp_server.tooling.screening import crypto as screening_crypto
from app.mcp_server.tooling.screening import enrichment as screening_enrichment
from app.mcp_server.tooling.screening import kr as screening_kr
from app.mcp_server.tooling.screening import us as screening_us
from app.services import naver_finance
from tests._mcp_tooling_support import _patch_runtime_attr

ToolFunc = Callable[..., Awaitable[Any]]


class _TvCondition:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _TvCondition) and self.label == other.label

    def __and__(self, other: object) -> object:
        raise AssertionError("crypto filters must not be combined with '&'")


class _TvField:
    def __init__(self, label: str) -> None:
        self.label = label

    def __eq__(self, other: object) -> bool:
        return cast(bool, cast(object, _TvCondition(f"{self.label}=={other}")))

    def isin(self, other: object) -> _TvCondition:
        values = list(cast(Any, other))
        return _TvCondition(f"{self.label} in {values}")


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, ToolFunc] = {}

    def tool(self, name: str, description: str):
        _ = description

        def decorator(func: ToolFunc) -> ToolFunc:
            self.tools[name] = func
            return func

        return decorator


def build_tools() -> dict[str, ToolFunc]:
    mcp = DummyMCP()
    register_all_tools(cast(Any, mcp))
    return mcp.tools


@pytest.fixture
def fake_crypto_tvscreener_module() -> SimpleNamespace:
    return SimpleNamespace(
        CryptoField=SimpleNamespace(
            NAME=_TvField("name"),
            DESCRIPTION=_TvField("description"),
            PRICE=_TvField("price"),
            CHANGE_PERCENT=_TvField("change_percent"),
            RELATIVE_STRENGTH_INDEX_14=_TvField("rsi14"),
            AVERAGE_DIRECTIONAL_INDEX_14=_TvField("adx14"),
            VOLUME_24H_IN_USD=_TvField("volume24h"),
            VALUE_TRADED=_TvField("value_traded"),
            MARKET_CAP=_TvField("market_cap"),
            EXCHANGE=_TvField("exchange"),
        )
    )


@pytest.fixture
def mock_krx_stocks():
    """Mock KRX stock data (market_cap in 억원)."""
    return [
        {
            "code": "005930",
            "name": "삼성전자",
            "close": 80000.0,
            "change_rate": 2.5,
            "change_price": 2000,
            "market": "KOSPI",
            "market_cap": 4800000,  # 480조원 = 4,800,000억원
        },
        {
            "code": "000660",
            "name": "SK하이닉스",
            "close": 150000.0,
            "change_rate": -1.2,
            "change_price": -1800,
            "market": "KOSPI",
            "market_cap": 150000,  # 15조원 = 150,000억원
        },
    ]


@pytest.fixture
def mock_krx_etfs():
    """Mock KRX ETF data (market_cap in 억원)."""
    return [
        {
            "code": "069500",
            "name": "KODEX 200",
            "close": 45000.0,
            "market": "KOSPI",
            "market_cap": 45000,  # 4.5조원 = 45,000억원
            "index_name": "KOSPI 200",
        },
        {
            "code": "114800",
            "name": "KODEX 반도체",
            "close": 12000.0,
            "market": "KOSPI",
            "market_cap": 1200,  # 1.2조원 = 1,200억원
            "index_name": "Wise 반도체지수",
        },
    ]


@pytest.fixture
def mock_valuation_data():
    """Mock valuation data from KRX."""
    return {
        "005930": {"per": 12.5, "pbr": 1.2, "dividend_yield": 0.0256},
        "000660": {"per": None, "pbr": None, "dividend_yield": None},
        "035420": {"per": 0, "pbr": 0.8, "dividend_yield": 0.035},
    }


# ----------------------------------------------------------------------
# Smoke Test
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_screen_stocks_smoke(monkeypatch):
    """Smoke test for screen_stocks tool registration and basic invocation."""
    tools = build_tools()

    assert "screen_stocks" in tools

    mock_krx_stocks = [
        {
            "code": "005930",
            "name": "삼성전자",
            "close": 80000.0,
            "market": "KOSPI",
            "market_cap": 480000000000000,
        },
        {
            "code": "000660",
            "name": "SK하이닉스",
            "close": 150000.0,
            "market": "KOSPI",
            "market_cap": 15000000000000,
        },
    ]

    async def mock_fetch_stock_all_cached(market):
        return mock_krx_stocks

    async def mock_fetch_etf_all_cached():
        return []

    _patch_runtime_attr(
        monkeypatch, "fetch_stock_all_cached", mock_fetch_stock_all_cached
    )
    _patch_runtime_attr(monkeypatch, "fetch_etf_all_cached", mock_fetch_etf_all_cached)

    result = await tools["screen_stocks"](market="kr", limit=5)

    assert isinstance(result, dict)
    assert "results" in result
    assert "total_count" in result
    assert "returned_count" in result
    assert "filters_applied" in result
    assert "timestamp" in result
    assert "market" in result

    # Verify filters_applied includes required keys
    assert "market" in result["filters_applied"]
    assert "sort_by" in result["filters_applied"]
    assert "sort_order" in result["filters_applied"]

    assert isinstance(result["results"], list)


class TestScreenStocksKRRegression:
    """Regression tests for KR market edge paths."""

    @pytest.mark.asyncio
    async def test_kr_change_rate_sort_desc(self, mock_krx_stocks, monkeypatch):
        """KR change_rate sorting should preserve positive/negative ordering."""

        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kospi"
            assert kwargs["sort_by"] == "change_rate"
            assert kwargs["sort_order"] == "desc"
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 80000.0,
                        "change_percent": 2.5,
                        "volume": 1000.0,
                        "market_cap": 4_800_000,
                        "adx": 20.0,
                        "market": "KOSPI",
                    },
                    {
                        "symbol": "000660",
                        "name": "SK hynix Inc.",
                        "price": 150000.0,
                        "change_percent": -1.2,
                        "volume": 900.0,
                        "market_cap": 150_000,
                        "adx": 18.0,
                        "market": "KOSPI",
                    },
                ],
                "count": 2,
                "filters_applied": {"sort_by": "change_rate", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="desc",
            limit=20,
        )

        assert result["returned_count"] == 2
        assert result["results"][0]["change_rate"] == 2.5
        assert result["results"][-1]["change_rate"] == -1.2

    @pytest.mark.asyncio
    async def test_submarket_routing_kospi_and_kosdaq(self, monkeypatch):
        """KOSPI/KOSDAQ should call only STK/KSQ source respectively."""

        calls: list[str] = []

        async def mock_fetch_stock_all_cached(market):
            calls.append(market)
            return []

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        await tools["screen_stocks"](
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )
        assert calls == ["STK"]

        calls.clear()
        await tools["screen_stocks"](
            market="kosdaq",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )
        assert calls == ["KSQ"]

    @pytest.mark.asyncio
    async def test_kosdaq_skips_etf_fetch_when_asset_type_none(self, monkeypatch):
        """kosdaq should not fetch ETFs when asset_type is None."""

        etf_called = False

        async def mock_fetch_stock_all_cached(market):
            return []

        async def mock_fetch_etf_all_cached():
            nonlocal etf_called
            etf_called = True
            return []

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kosdaq",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["market"] == "kosdaq"
        assert not etf_called

    @pytest.mark.asyncio
    async def test_kr_batch_valuation_merge(
        self, mock_krx_stocks, mock_valuation_data, monkeypatch
    ):
        """Batch valuation data should be merged into legacy KR results."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        async def mock_fetch_valuation_all_cached(market):
            return mock_valuation_data

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr,
            "_can_use_tvscreener_stock_path",
            lambda **kwargs: False,
        )
        monkeypatch.setattr(
            screening_kr,
            "_get_tvscreener_stock_capability_snapshot",
            AsyncMock(return_value=object()),
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        merged = {item["code"]: item for item in result["results"]}
        assert merged["005930"]["per"] == 12.5
        assert merged["005930"]["pbr"] == 1.2
        assert merged["005930"]["dividend_yield"] == 0.0256
        assert merged["000660"]["per"] is None
        assert merged["000660"]["pbr"] is None
        assert merged["000660"]["dividend_yield"] is None

    @pytest.mark.asyncio
    async def test_kr_valuation_fetch_failure_is_graceful(
        self, mock_krx_stocks, monkeypatch
    ):
        """Valuation fetch failure should not break KR screening."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        async def mock_fetch_valuation_all_cached(market):
            raise RuntimeError("KRX valuation temporary failure")

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert "error" not in result
        assert result["returned_count"] == 2


@pytest.fixture
def mock_yfinance_screen():
    """Mock yfinance.screen function."""

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


class TestScreenStocksKR:
    """Test KR market functionality."""

    @pytest.mark.asyncio
    async def test_kr_stocks_default(self, mock_krx_stocks, monkeypatch):
        """Test KR stock screening with default parameters."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert "results" in result
        assert "total_count" in result
        assert "returned_count" in result
        assert "filters_applied" in result
        assert "timestamp" in result
        assert result["market"] == "kr"

    @pytest.mark.asyncio
    async def test_kr_etfs_default(self, mock_krx_etfs, monkeypatch):
        """Test KR ETF screening with default parameters."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="etf",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["market"] == "kr"
        assert len(result["results"]) > 0

    @pytest.mark.asyncio
    async def test_kr_auto_etf_on_category(self, mock_krx_etfs, monkeypatch):
        """Test KR auto-limits to ETFs when category is specified."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
            category="반도체",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["asset_type"] == "etf"

    @pytest.mark.asyncio
    async def test_kr_etn_not_supported(self):
        """Test KR ETN (Exchange Traded Note) raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="not supported|ETN"):
            await tools["screen_stocks"](
                market="kr",
                asset_type="etn",
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=20,
            )


class TestScreenStocksUS:
    """Test US market functionality."""

    @pytest.mark.asyncio
    async def test_us_stocks_default(self, mock_yfinance_screen, monkeypatch):
        """Test US stock screening with default parameters."""

        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["market"] == "us"
        assert len(result["results"]) >= 0
        assert "error" not in result, f"Unexpected error: {result.get('error')}"


class TestScreenStocksTvScreenerContract:
    @pytest.mark.asyncio
    async def test_kr_tvscreener_path_preserves_public_response_contract(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["sort_by"] == "volume"
            assert kwargs["sort_order"] == "desc"
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 15000000.0,
                        "market_cap": 4800000,
                        "per": 12.5,
                        "pbr": 1.2,
                        "dividend_yield": 0.0256,
                        "rsi": 28.1,
                        "adx": 24.8,
                        "market": "KOSPI",
                    }
                ],
                "count": 3,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "limit": 20,
                    "max_rsi": 30.0,
                    "min_market_cap": 300000,
                    "max_per": 15.0,
                    "max_pbr": 2.0,
                    "min_dividend_yield": 0.02,
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=300000,
            max_per=15.0,
            max_pbr=2.0,
            min_dividend_yield=0.02,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert set(result) >= {
            "results",
            "total_count",
            "returned_count",
            "filters_applied",
            "market",
            "timestamp",
            "meta",
        }
        assert result["total_count"] == 3
        assert result["returned_count"] == 1
        assert result["results"][0]["code"] == "005930"
        assert result["results"][0]["close"] == 70000.0
        assert result["results"][0]["change_rate"] == 2.5
        assert result["results"][0]["market"] == "KOSPI"
        assert result["results"][0]["market_cap"] == 4800000
        assert result["results"][0]["per"] == 12.5
        assert result["results"][0]["pbr"] == 1.2
        assert result["results"][0]["dividend_yield"] == 0.0256
        assert result["results"][0]["adx"] == 24.8
        assert result["filters_applied"]["sort_order"] == "desc"
        assert result["filters_applied"]["min_market_cap"] == 300000
        assert result["filters_applied"]["max_per"] == 15.0
        assert result["filters_applied"]["max_pbr"] == 2.0
        assert result["filters_applied"]["min_dividend_yield"] == 0.02
        assert result["meta"]["source"] == "tvscreener"
        assert result["meta"]["rsi_enrichment"]["error_samples"] == []

    @pytest.mark.asyncio
    async def test_us_tvscreener_path_preserves_public_response_contract(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["sort_by"] == "volume"
            assert kwargs["sort_order"] == "asc"
            assert kwargs["asset_type"] is None
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2800000000000,
                        "per": 28.5,
                        "dividend_yield": 0.005,
                        "rsi": 35.2,
                        "adx": 31.4,
                    }
                ],
                "count": 4,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "asc",
                    "limit": 20,
                    "max_rsi": 40.0,
                    "min_market_cap": 1000000000,
                    "max_per": 30.0,
                    "min_dividend_yield": 0.004,
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=1000000000,
            max_per=30.0,
            min_dividend_yield=0.004,
            max_rsi=None,
            sort_by="volume",
            sort_order="asc",
            limit=20,
        )

        assert result["total_count"] == 4
        assert result["returned_count"] == 1
        assert result["results"][0]["code"] == "AAPL"
        assert result["results"][0]["close"] == 175.5
        assert result["results"][0]["change_rate"] == 1.2
        assert result["results"][0]["market"] == "us"
        assert result["results"][0]["market_cap"] == 2800000000000
        assert result["results"][0]["per"] == 28.5
        assert result["results"][0]["dividend_yield"] == 0.005
        assert result["results"][0]["adx"] == 31.4
        assert result["filters_applied"]["sort_order"] == "asc"
        assert result["filters_applied"]["min_market_cap"] == 1000000000
        assert result["filters_applied"]["max_per"] == 30.0
        assert result["filters_applied"]["min_dividend_yield"] == 0.004
        assert result["meta"]["source"] == "tvscreener"

    @pytest.mark.asyncio
    async def test_kr_default_stock_request_uses_tvscreener_without_legacy_rsi_path(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["category"] is None
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 15000000.0,
                        "market_cap": 4800000,
                        "rsi": 41.2,
                        "adx": 23.5,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs):
            raise AssertionError(
                "legacy KR path should not run for default stock requests"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
            fail_legacy_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["rsi"] == 41.2
        assert result["results"][0]["adx"] == 23.5
        assert result["meta"]["rsi_enrichment"]["error_samples"] == []

    @pytest.mark.asyncio
    async def test_us_default_stock_request_uses_tvscreener_without_legacy_path(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["category"] is None
            assert kwargs["max_rsi"] is None
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2800000000000,
                        "rsi": 35.2,
                        "adx": 31.4,
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError(
                "legacy US path should not run for default stock requests"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["adx"] == 31.4

    @pytest.mark.asyncio
    async def test_kr_tvscreener_enriched_rows_preserve_sector_and_analyst_fields(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 174.4,
                        "change_percent": 2.1,
                        "volume": 44_000_000.0,
                        "market_cap": 4_200_000.0,
                        "per": 61.3,
                        "pbr": 18.7,
                        "dividend_yield": 0.004,
                        "market": "KOSPI",
                        "sector": "Electronic Technology",
                        "analyst_buy": 65,
                        "analyst_hold": 4,
                        "analyst_sell": 1,
                        "avg_target": 269.16,
                        "upside_pct": 54.33,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "market": "kr",
                    "asset_type": "stock",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        first = result["results"][0]
        assert first["sector"] == "Electronic Technology"
        assert first["analyst_buy"] == 65
        assert first["analyst_hold"] == 4
        assert first["analyst_sell"] == 1
        assert first["avg_target"] == pytest.approx(269.16)
        assert first["upside_pct"] == pytest.approx(54.33)
        assert first["market_cap"] == pytest.approx(4_200_000.0)
        assert first["per"] == pytest.approx(61.3)
        assert first["pbr"] == pytest.approx(18.7)
        assert first["dividend_yield"] == pytest.approx(0.004)

    @pytest.mark.asyncio
    async def test_us_category_and_analyst_filter_stay_on_tvscreener_without_network_enrichment(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["category"] == "Technology"
            assert kwargs["limit"] == 1
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2800000000000,
                        "rsi": 35.2,
                        "adx": 31.4,
                        "market": "us",
                        "sector": "Technology",
                        "analyst_buy": 18,
                        "analyst_hold": 4,
                        "analyst_sell": 1,
                        "avg_target": 210.0,
                        "upside_pct": 19.66,
                    },
                    {
                        "symbol": "IBM",
                        "name": "IBM",
                        "price": 190.0,
                        "change_percent": 0.4,
                        "volume": 12000000.0,
                        "market_cap": 170000000000,
                        "rsi": 42.0,
                        "adx": 22.0,
                        "market": "us",
                        "sector": "Technology",
                        "analyst_buy": 7,
                        "analyst_hold": 8,
                        "analyst_sell": 2,
                        "avg_target": 195.0,
                        "upside_pct": 2.63,
                    },
                ],
                "count": 2,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": "Technology",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError(
                "legacy US path should not run for category/analyst tvscreener requests"
            )

        async def fail_enrichment(symbol: str, **kwargs):
            raise AssertionError(
                f"network enrichment should not run for pre-enriched tvscreener row {symbol}"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )
        monkeypatch.setattr(
            screening_us,
            "_can_use_tvscreener_stock_path",
            lambda **kwargs: True,
        )
        monkeypatch.setattr(
            screening_us,
            "_get_tvscreener_stock_capability_snapshot",
            AsyncMock(return_value=object()),
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.enrichment._fetch_screen_enrichment_us",
            fail_enrichment,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            min_analyst_buy=10,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["total_count"] == 1
        assert result["returned_count"] == 1
        assert result["filters_applied"]["category"] == "Technology"
        assert result["filters_applied"]["min_analyst_buy"] == 10
        first = result["results"][0]
        assert first["code"] == "AAPL"
        assert first["sector"] == "Technology"
        assert first["analyst_buy"] == 18
        assert first["analyst_hold"] == 4
        assert first["analyst_sell"] == 1
        assert first["avg_target"] == 210.0
        assert first["upside_pct"] == 19.66

    @pytest.mark.asyncio
    async def test_us_enrichment_fallback_only_runs_for_rows_missing_tvscreener_fields(
        self, monkeypatch
    ):
        fetch_enrichment = AsyncMock(
            return_value={
                "sector": "Software",
                "analyst_buy": 16,
                "analyst_hold": 5,
                "analyst_sell": 1,
                "avg_target": 470.0,
                "upside_pct": 14.63,
            }
        )
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fetch_enrichment,
        )

        (
            rows,
            warnings,
        ) = await analysis_screen_core._decorate_screen_rows_with_equity_enrichment(
            [
                {
                    "code": "AAPL",
                    "market": "us",
                    "sector": "Technology",
                    "analyst_buy": 20,
                    "analyst_hold": 3,
                    "analyst_sell": 1,
                    "avg_target": 225.0,
                    "upside_pct": 11.8,
                },
                {
                    "code": "MSFT",
                    "market": "us",
                    "sector": None,
                    "analyst_buy": None,
                    "analyst_hold": None,
                    "analyst_sell": None,
                    "avg_target": None,
                    "upside_pct": None,
                },
            ]
        )

        assert warnings == []
        assert fetch_enrichment.await_count == 1
        assert fetch_enrichment.await_args is not None
        assert fetch_enrichment.await_args.args[0] == "MSFT"
        assert rows[0]["sector"] == "Technology"
        assert rows[0]["analyst_buy"] == 20
        assert rows[0]["avg_target"] == 225.0
        assert rows[1]["sector"] == "Software"
        assert rows[1]["analyst_buy"] == 16
        assert rows[1]["analyst_hold"] == 5
        assert rows[1]["analyst_sell"] == 1
        assert rows[1]["avg_target"] == 470.0
        assert rows[1]["upside_pct"] == 14.63

    @pytest.mark.asyncio
    async def test_us_enrichment_fallback_preserves_existing_tvscreener_values(
        self, monkeypatch
    ):
        fetch_enrichment = AsyncMock(
            return_value={
                "sector": None,
                "analyst_buy": 0,
                "analyst_hold": 0,
                "analyst_sell": 0,
                "avg_target": 220.0,
                "upside_pct": 10.0,
            }
        )
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fetch_enrichment,
        )

        (
            rows,
            warnings,
        ) = await analysis_screen_core._decorate_screen_rows_with_equity_enrichment(
            [
                {
                    "code": "AAPL",
                    "market": "us",
                    "sector": "Technology",
                    "analyst_buy": 18,
                    "analyst_hold": 4,
                    "analyst_sell": 1,
                    "avg_target": None,
                    "upside_pct": None,
                    "close": 200.0,
                }
            ]
        )

        assert warnings == []
        assert fetch_enrichment.await_count == 1
        assert rows[0]["sector"] == "Technology"
        assert rows[0]["analyst_buy"] == 18
        assert rows[0]["analyst_hold"] == 4
        assert rows[0]["analyst_sell"] == 1
        assert rows[0]["avg_target"] == 220.0
        assert rows[0]["upside_pct"] == 10.0

    @pytest.mark.asyncio
    async def test_us_category_preserves_acronym_case_for_tvscreener_filter(
        self, monkeypatch
    ):
        captured: dict[str, Any] = {}

        async def mock_screen_us_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "AI",
                        "name": "C3.ai, Inc.",
                        "price": 30.0,
                        "change_percent": 1.5,
                        "volume": 1000.0,
                        "market_cap": 4_000_000_000.0,
                        "market": "us",
                        "sector": "AI",
                        "analyst_buy": 9,
                        "analyst_hold": 4,
                        "analyst_sell": 1,
                        "avg_target": 36.0,
                        "upside_pct": 20.0,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": "AI",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("legacy US path should not run for AI category")

        async def fail_enrichment(symbol: str, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                f"network enrichment should not run for pre-enriched tvscreener row {symbol}"
            )

        monkeypatch.setattr(
            screening_us,
            "_screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(screening_us, "_screen_us", fail_legacy_us)
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fail_enrichment,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="AI",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

        assert captured["category"] == "AI"
        assert result["filters_applied"]["category"] == "AI"
        assert result["filters_applied"]["sector"] == "AI"
        assert result["results"][0]["sector"] == "AI"

    @pytest.mark.asyncio
    async def test_us_category_lowercase_technology_canonicalized_for_tvscreener(
        self, monkeypatch
    ):
        captured: dict[str, Any] = {}

        async def mock_screen_us_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 200.0,
                        "change_percent": 0.5,
                        "volume": 50_000.0,
                        "market_cap": 3_000_000_000_000.0,
                        "market": "us",
                        "sector": "Technology",
                        "analyst_buy": 30,
                        "analyst_hold": 5,
                        "analyst_sell": 1,
                        "avg_target": 250.0,
                        "upside_pct": 25.0,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": "Technology",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                "legacy US path should not run for technology category"
            )

        async def fail_enrichment(symbol: str, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                f"network enrichment should not run for pre-enriched tvscreener row {symbol}"
            )

        monkeypatch.setattr(
            screening_us,
            "_screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(screening_us, "_screen_us", fail_legacy_us)
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            fail_enrichment,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=1,
        )

        assert captured["category"] == "Technology"
        assert result["filters_applied"]["sector"] == "Technology"
        assert result["results"][0]["sector"] == "Technology"

    @pytest.mark.asyncio
    async def test_kr_stock_request_with_max_rsi_still_uses_tvscreener(
        self, monkeypatch
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == "kr"
            assert kwargs["asset_type"] == "stock"
            assert kwargs["max_rsi"] == 35.0
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 1.1,
                        "volume": 12345.0,
                        "market_cap": 4_800_000,
                        "rsi": 32.0,
                        "adx": 21.5,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 35.0,
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs):
            raise AssertionError(
                "legacy KR path should not run when max_rsi is provided"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
            fail_legacy_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=35.0,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["rsi"] == 32.0
        assert result["results"][0]["adx"] == 21.5

    @pytest.mark.asyncio
    async def test_us_stock_request_with_max_rsi_still_uses_tvscreener(
        self, monkeypatch
    ):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["asset_type"] is None
            assert kwargs["max_rsi"] == 40.0
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 175.5,
                        "change_percent": 1.2,
                        "volume": 75000000.0,
                        "market_cap": 2_800_000_000_000,
                        "rsi": 35.2,
                        "adx": 31.4,
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 40.0,
                },
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs):
            raise AssertionError(
                "legacy US path should not run when max_rsi is provided"
            )

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            fail_legacy_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=40.0,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["rsi"] == 35.2
        assert result["results"][0]["adx"] == 31.4

    @pytest.mark.asyncio
    async def test_us_tvscreener_error_falls_back_to_legacy_path(self, monkeypatch):
        async def mock_screen_us_via_tvscreener(**kwargs):
            return {
                "stocks": [],
                "count": 0,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": None,
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 40.0,
                },
                "source": "tvscreener",
                "error": "tvscreener PE field unavailable",
            }

        async def mock_screen_us(**kwargs):
            assert kwargs["market"] == "us"
            assert kwargs["max_rsi"] == 40.0
            return {
                "results": [
                    {
                        "code": "AAPL",
                        "name": "Apple Inc.",
                        "close": 175.5,
                        "change_rate": 1.2,
                        "volume": 75000000.0,
                        "market": "us",
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "us",
                    "asset_type": None,
                    "category": None,
                    "sort_by": "volume",
                    "sort_order": "desc",
                    "max_rsi": 40.0,
                },
                "market": "us",
                "timestamp": "2026-03-07T00:00:00+00:00",
                "meta": {"source": "legacy"},
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us",
            mock_screen_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=40.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["results"][0]["code"] == "AAPL"
        assert result["market"] == "us"
        assert result["meta"]["source"] == "legacy"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("market", ["kospi", "kosdaq"])
    async def test_kr_tvscreener_path_passes_requested_submarket(
        self, monkeypatch, market
    ):
        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["market"] == market
            return {
                "stocks": [
                    {
                        "symbol": "005930" if market == "kospi" else "035720",
                        "name": "stub",
                        "price": 1.0,
                        "change_percent": 0.1,
                        "volume": 100.0,
                        "market": market.upper(),
                        "rsi": 25.0,
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market=market,
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=30.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["results"][0]["market"] == market.upper()
        assert result["filters_applied"]["market"] == market

    @pytest.mark.asyncio
    async def test_us_category_with_max_rsi_falls_back_to_legacy_path(
        self, mock_yfinance_screen, monkeypatch
    ):
        import yfinance as yf

        async def fail_if_called(**kwargs):
            raise AssertionError(
                "tvscreener path should not run for market_cap sorting"
            )

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            fail_if_called,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category="Technology",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=40.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["market"] == "us"
        assert "results" in result

    @pytest.mark.asyncio
    async def test_kr_category_with_max_rsi_falls_back_to_legacy_path(
        self, monkeypatch
    ):
        async def fail_if_called(**kwargs):
            raise AssertionError(
                "tvscreener path should not run for category-based KR screening"
            )

        async def mock_screen_kr(**kwargs):
            return {
                "results": [{"code": "069500", "name": "KODEX 200", "market": "kr"}],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "kr",
                    "asset_type": "etf",
                    "category": "반도체",
                    "sort_by": "volume",
                    "sort_order": "desc",
                },
                "market": "kr",
                "meta": {"rsi_enrichment": {}},
                "timestamp": "2026-03-07T00:00:00+00:00",
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            fail_if_called,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr",
            mock_screen_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
            category="반도체",
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=30.0,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result["filters_applied"]["asset_type"] == "etf"
        assert result["filters_applied"]["category"] == "반도체"


@pytest.fixture
def mock_upbit_coins():
    """Mock Upbit top traded coins data."""
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
        import pandas as pd

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


class TestScreenStocksCrypto:
    """Test Crypto market functionality."""

    @pytest.mark.asyncio
    async def test_crypto_default_restores_public_contract_on_tvscreener_success(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["Bitcoin TV", "Ethereum TV", "Ripple TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [-0.01, -0.02, -0.31],
                "relative_strength_index_14": [45.5, 32.1, 28.2],
                "average_directional_index_14": [25.3, 18.7, 42.1],
                "volume_24h_in_usd": [156_000_000.0, 95_000_000.0, 44_000_000.0],
                "value_traded": [900_000_000_000.0, 1_200_000_000_000.0, 700_000_000.0],
                "market_cap": [
                    2_500_000_000_000_000.0,
                    500_000_000_000_000.0,
                    50_000_000_000_000.0,
                ],
                "exchange": ["UPBIT", "UPBIT", "UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, Any]]:
            assert market_codes == ["KRW-BTC", "KRW-ETH", "KRW-XRP"]
            return [
                {"market": "KRW-BTC", "acc_trade_volume_24h": 12_345.0},
                {"market": "KRW-ETH", "acc_trade_volume_24h": 54_321.0},
                {"market": "KRW-XRP", "acc_trade_volume_24h": 99_999.0},
            ]

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return {"KRW-ETH"}

        async def mock_market_cap_cache_get() -> dict[str, Any]:
            return {
                "data": {
                    "BTC": {
                        "market_cap": 3_000_000_000_000_000,
                        "market_cap_rank": 1,
                    }
                },
                "cached": True,
                "age_seconds": 1.5,
                "stale": False,
                "error": None,
            }

        async def mock_fetch_ohlcv(
            symbol: str, market_type: str, count: int
        ) -> pd.DataFrame:
            assert market_type == "crypto"
            assert count == 50
            close = [100.0 + i for i in range(50)]
            volume = [1_000.0] * 49 + [1_500.0]
            return pd.DataFrame(
                {
                    "open": close,
                    "high": [value + 10.0 for value in close],
                    "low": [value - 10.0 for value in close],
                    "close": close,
                    "volume": volume,
                }
            )

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_market_display_names",
            AsyncMock(
                return_value={
                    "KRW-BTC": {
                        "korean_name": "비트코인",
                        "english_name": "Bitcoin",
                    },
                    "KRW-ETH": {
                        "korean_name": "이더리움",
                        "english_name": "Ethereum",
                    },
                }
            ),
            raising=False,
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_order="desc",
            limit=1,
        )

        query_kwargs = tv_service.query_crypto_screener.await_args.kwargs
        assert query_kwargs["limit"] == 50
        assert (
            fake_crypto_tvscreener_module.CryptoField.DESCRIPTION
            in query_kwargs["columns"]
        )
        assert (
            fake_crypto_tvscreener_module.CryptoField.MARKET_CAP
            in query_kwargs["columns"]
        )

        assert result is not None
        assert result["market"] == "crypto"
        assert len(result["results"]) == 1
        assert result["filters_applied"]["sort_by"] == "rsi"
        assert result["filters_applied"]["sort_order"] == "asc"
        assert result["meta"]["source"] == "tvscreener"
        assert result["meta"]["filtered_by_warning"] == 1
        assert result["meta"]["filtered_by_crash"] == 1

        first = result["results"][0]
        assert first["symbol"] == "KRW-BTC"
        assert first["name"] == "비트코인"
        assert first["trade_amount_24h"] == 900_000_000_000.0
        assert first["volume_24h"] == 12_345.0
        assert first["market_cap"] == 3_000_000_000_000_000
        assert first["market_cap_rank"] == 1
        assert first["rsi_bucket"] == 45
        assert first["market_warning"] is None
        assert "volume_ratio" in first
        assert "candle_type" in first
        assert "plus_di" in first
        assert "minus_di" in first
        assert "volume" not in first

    @pytest.mark.asyncio
    async def test_crypto_sort_by_volume_raises_error(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        with pytest.raises(ValueError, match=".*does not support sorting by.*volume.*"):
            await tools["screen_stocks"](
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_crypto_trade_amount_sorting_uses_24h_trade_value(self, monkeypatch):
        async def mock_fetch_top_traded_coins(fiat):
            assert fiat == "KRW"
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 9_999_999,
                    "acc_trade_price_24h": 1_000,
                },
                {
                    "market": "KRW-ETH",
                    "korean_name": "이더리움",
                    "trade_price": 5_000_000,
                    "signed_change_rate": 0.02,
                    "acc_trade_volume_24h": 1,
                    "acc_trade_price_24h": 10_000,
                },
                {
                    "market": "KRW-SOL",
                    "korean_name": "솔라나",
                    "trade_price": 200_000,
                    "signed_change_rate": 0.03,
                    "acc_trade_volume_24h": 100,
                    "acc_trade_price_24h": 5_000,
                },
            ]

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=3,
        )

        symbols = [item["symbol"] for item in result["results"]]
        assert symbols == ["KRW-ETH", "KRW-SOL", "KRW-BTC"]
        assert all("trade_amount_24h" in item for item in result["results"])
        assert all("volume" not in item for item in result["results"])


class TestScreenStocksFundamentalsExpansion:
    @pytest.mark.asyncio
    async def test_screen_stocks_accepts_new_public_fundamentals_contract(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def fake_screen(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "results": [
                    {
                        "code": "AAPL",
                        "name": "Apple Inc.",
                        "sector": "Technology",
                        "analyst_buy": 18,
                        "analyst_hold": 6,
                        "analyst_sell": 1,
                        "avg_target": 245.0,
                        "upside_pct": 12.4,
                    }
                ],
                "total_count": 1,
                "returned_count": 1,
                "filters_applied": {
                    "market": "us",
                    "sector": "Technology",
                    "min_analyst_buy": 10,
                    "min_dividend_input": 2.5,
                    "min_dividend_normalized": 0.025,
                },
                "market": "us",
                "timestamp": "2026-03-11T00:00:00Z",
                "meta": {"source": "fundamentals-expansion"},
            }

        monkeypatch.setattr(analysis_screening, "screen_stocks_unified", fake_screen)

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            sector="Technology",
            min_analyst_buy=10,
            min_dividend=2.5,
            limit=5,
        )

        assert captured["sector"] == "Technology"
        assert captured["min_analyst_buy"] == 10
        assert captured["min_dividend"] == 2.5
        first = result["results"][0]
        assert first["sector"] == "Technology"
        assert first["analyst_buy"] == 18
        assert first["analyst_hold"] == 6
        assert first["analyst_sell"] == 1
        assert first["avg_target"] == 245.0
        assert first["upside_pct"] == 12.4
        assert result["filters_applied"]["sector"] == "Technology"
        assert result["filters_applied"]["min_analyst_buy"] == 10
        assert result["filters_applied"]["min_dividend_input"] == 2.5
        assert result["filters_applied"]["min_dividend_normalized"] == 0.025

    @pytest.mark.asyncio
    async def test_us_plain_tvscreener_requests_enrich_only_limited_rows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        enriched_symbols: list[str] = []

        async def mock_screen_us_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 200.0,
                        "change_percent": 1.0,
                        "volume": 300.0,
                        "market_cap": 3_000_000_000_000.0,
                    },
                    {
                        "symbol": "MSFT",
                        "name": "Microsoft Corp.",
                        "price": 300.0,
                        "change_percent": 0.5,
                        "volume": 200.0,
                        "market_cap": 2_500_000_000_000.0,
                    },
                    {
                        "symbol": "NVDA",
                        "name": "NVIDIA Corp.",
                        "price": 400.0,
                        "change_percent": 0.2,
                        "volume": 100.0,
                        "market_cap": 2_000_000_000_000.0,
                    },
                ],
                "count": 3,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_us(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError(
                "legacy US path should not run for plain stock requests"
            )

        async def mock_fetch_screen_enrichment_us(
            symbol: str, **kwargs: Any
        ) -> dict[str, Any]:
            enriched_symbols.append(symbol)
            return {
                "sector": "Technology",
                "analyst_buy": 12,
                "analyst_hold": 3,
                "analyst_sell": 1,
                "avg_target": 250.0,
                "upside_pct": 10.0,
            }

        monkeypatch.setattr(
            screening_us,
            "_screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )
        monkeypatch.setattr(screening_us, "_screen_us", fail_legacy_us)
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_us",
            mock_fetch_screen_enrichment_us,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            asset_type="stock",
            sort_by="volume",
            sort_order="desc",
            limit=2,
        )

        assert captured["limit"] == 2
        assert enriched_symbols == ["AAPL", "MSFT"]
        assert result["returned_count"] == 2
        assert result["meta"]["source"] == "tvscreener"
        assert result["results"][0]["sector"] == "Technology"
        assert result["results"][0]["analyst_buy"] == 12
        assert result["results"][0]["avg_target"] == 250.0
        assert result["results"][1]["upside_pct"] == 10.0

    @pytest.mark.asyncio
    async def test_kr_sector_and_analyst_filters_apply_after_overfetch_enrichment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def mock_screen_kr_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 600.0,
                        "market_cap": 4_800_000.0,
                        "market": "KOSPI",
                    },
                    {
                        "symbol": "000660",
                        "name": "SK hynix Inc.",
                        "price": 150000.0,
                        "change_percent": 1.5,
                        "volume": 500.0,
                        "market_cap": 1_500_000.0,
                        "market": "KOSPI",
                    },
                    {
                        "symbol": "035420",
                        "name": "NAVER Corp.",
                        "price": 200000.0,
                        "change_percent": 1.0,
                        "volume": 400.0,
                        "market_cap": 900_000.0,
                        "market": "KOSPI",
                    },
                    {
                        "symbol": "051910",
                        "name": "LG Chem, Ltd.",
                        "price": 300000.0,
                        "change_percent": 0.8,
                        "volume": 300.0,
                        "market_cap": 800_000.0,
                        "market": "KOSPI",
                    },
                ],
                "count": 4,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("legacy KR path should not run for stock requests")

        enrichment_map = {
            "005930": {
                "sector": "전기전자",
                "analyst_buy": 8,
                "analyst_hold": 3,
                "analyst_sell": 1,
                "avg_target": 90000.0,
                "upside_pct": 12.0,
            },
            "000660": {
                "sector": "반도체",
                "analyst_buy": 6,
                "analyst_hold": 2,
                "analyst_sell": 0,
                "avg_target": 180000.0,
                "upside_pct": 20.0,
            },
            "035420": {
                "sector": "반도체",
                "analyst_buy": 11,
                "analyst_hold": 1,
                "analyst_sell": 0,
                "avg_target": 250000.0,
                "upside_pct": 25.0,
            },
            "051910": {
                "sector": "반도체",
                "analyst_buy": 14,
                "analyst_hold": 0,
                "analyst_sell": 0,
                "avg_target": 350000.0,
                "upside_pct": 16.0,
            },
        }

        async def mock_fetch_screen_enrichment_kr(symbol: str) -> dict[str, Any]:
            return enrichment_map[symbol]

        monkeypatch.setattr(
            screening_kr,
            "_screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(screening_kr, "_screen_kr", fail_legacy_kr)
        monkeypatch.setattr(
            screening_enrichment,
            "_fetch_screen_enrichment_kr",
            mock_fetch_screen_enrichment_kr,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            sector="반도체",
            min_analyst_buy=10,
            limit=2,
        )

        assert captured["limit"] == 10
        assert result["meta"]["source"] == "tvscreener"
        assert result["returned_count"] == 2
        assert [item["code"] for item in result["results"]] == ["035420", "051910"]
        assert all(item["sector"] == "반도체" for item in result["results"])
        assert all(item["analyst_buy"] >= 10 for item in result["results"])
        assert result["filters_applied"]["sector"] == "반도체"
        assert result["filters_applied"]["min_analyst_buy"] == 10

    @pytest.mark.asyncio
    async def test_kr_min_analyst_buy_uses_partial_enrichment_when_profile_fails(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        async def mock_screen_kr_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            captured.update(kwargs)
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 600.0,
                        "market_cap": 4_800_000.0,
                        "market": "KOSPI",
                    },
                    {
                        "symbol": "000660",
                        "name": "SK hynix Inc.",
                        "price": 150000.0,
                        "change_percent": 1.5,
                        "volume": 500.0,
                        "market_cap": 1_500_000.0,
                        "market": "KOSPI",
                    },
                ],
                "count": 2,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("legacy KR path should not run for stock requests")

        async def mock_profile(symbol: str) -> dict[str, Any]:
            raise RuntimeError(f"profile unavailable for {symbol}")

        async def mock_opinions(symbol: str, limit: int) -> dict[str, Any]:
            assert limit == 10
            consensus_by_symbol = {
                "005930": {
                    "buy_count": 11,
                    "hold_count": 2,
                    "sell_count": 0,
                    "avg_target_price": 91000.0,
                    "upside_pct": 30.0,
                },
                "000660": {
                    "buy_count": 6,
                    "hold_count": 1,
                    "sell_count": 0,
                    "avg_target_price": 180000.0,
                    "upside_pct": 20.0,
                },
            }
            return {
                "symbol": symbol,
                "count": 3,
                "consensus": consensus_by_symbol[symbol],
            }

        monkeypatch.setattr(
            screening_kr,
            "_screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(screening_kr, "_screen_kr", fail_legacy_kr)
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_company_profile_finnhub",
            mock_profile,
            raising=False,
        )
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_investment_opinions_naver",
            mock_opinions,
            raising=False,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            min_analyst_buy=10,
            limit=2,
        )

        assert captured["limit"] == 2
        assert result["meta"]["source"] == "tvscreener"
        assert result["returned_count"] == 1
        assert [item["code"] for item in result["results"]] == ["005930"]
        assert result["results"][0]["sector"] is None
        assert result["results"][0]["analyst_buy"] == 11
        assert result["filters_applied"]["min_analyst_buy"] == 10
        assert not any(
            "profile unavailable" in warning for warning in result.get("warnings", [])
        )

    @pytest.mark.asyncio
    async def test_kr_screen_warns_when_both_enrichment_providers_fail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def mock_screen_kr_via_tvscreener(**kwargs: Any) -> dict[str, Any]:
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 70000.0,
                        "change_percent": 2.5,
                        "volume": 600.0,
                        "market_cap": 4_800_000.0,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {"sort_by": "volume", "sort_order": "desc"},
                "source": "tvscreener",
                "error": None,
            }

        async def fail_legacy_kr(**kwargs: Any) -> dict[str, Any]:
            raise AssertionError("legacy KR path should not run for stock requests")

        async def mock_profile(symbol: str) -> dict[str, Any]:
            raise RuntimeError(f"profile unavailable for {symbol}")

        async def mock_opinions(symbol: str, limit: int) -> dict[str, Any]:
            assert limit == 10
            raise RuntimeError(f"opinions unavailable for {symbol}")

        monkeypatch.setattr(
            screening_kr,
            "_screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )
        monkeypatch.setattr(screening_kr, "_screen_kr", fail_legacy_kr)
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_company_profile_finnhub",
            mock_profile,
            raising=False,
        )
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_investment_opinions_naver",
            mock_opinions,
            raising=False,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            min_analyst_buy=10,
            limit=1,
        )

        assert result["returned_count"] == 0
        assert result["total_count"] == 0
        assert any(
            "RuntimeError" in warning and "profile unavailable" in warning
            for warning in result.get("warnings", [])
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("kwargs", "pattern"),
        [
            ({"sector": "Layer1"}, ".*crypto.*sector.*"),
            ({"min_analyst_buy": 5}, ".*crypto.*min_analyst_buy.*"),
            ({"min_dividend": 2.0}, ".*crypto.*min_dividend.*"),
        ],
    )
    async def test_crypto_rejects_new_fundamentals_filters(
        self,
        kwargs: dict[str, Any],
        pattern: str,
    ) -> None:
        tools = build_tools()

        with pytest.raises(ValueError, match=pattern):
            await tools["screen_stocks"](market="crypto", limit=5, **kwargs)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("asset_type", ["etf", "etn"])
    async def test_kr_non_stock_requests_reject_min_analyst_buy(
        self, asset_type: str
    ) -> None:
        tools = build_tools()

        with pytest.raises(ValueError, match=".*min_analyst_buy.*"):
            await tools["screen_stocks"](
                market="kr",
                asset_type=asset_type,
                min_analyst_buy=3,
                limit=5,
            )

    @pytest.mark.asyncio
    async def test_sector_and_category_conflict_raises_error(self) -> None:
        tools = build_tools()

        with pytest.raises(ValueError, match=".*category.*sector.*"):
            await tools["screen_stocks"](
                market="us",
                category="Technology",
                sector="Semiconductors",
                limit=5,
            )

    @pytest.mark.asyncio
    async def test_crypto_per_filter_raises_error(self, mock_upbit_coins, monkeypatch):
        """Test crypto market raises ValueError for PER filter."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        with pytest.raises(ValueError, match=".*does not support.*max_per.*"):
            await tools["screen_stocks"](
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=20.0,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="trade_amount",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_crypto_dividend_filter_raises_error(
        self, mock_upbit_coins, monkeypatch
    ):
        """Test crypto market raises ValueError for dividend filter."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        with pytest.raises(
            ValueError, match=".*does not support.*min_dividend_yield.*"
        ):
            await tools["screen_stocks"](
                market="crypto",
                asset_type=None,
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=0.03,
                max_rsi=None,
                sort_by="trade_amount",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_kr_sort_by_rsi_succeeds(self, mock_krx_stocks, monkeypatch):
        """Test KR market allows sort_by='rsi' (tvscreener provides RSI)."""
        monkeypatch.setattr(
            screening_kr,
            "fetch_stock_all_cached",
            AsyncMock(return_value=mock_krx_stocks),
        )

        # Mock tvscreener capability to return usable
        async def mock_tvscreener_kr(*args, **kwargs):
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "short_code": "005930",
                        "code": "005930",
                        "name": "삼성전자",
                        "price": 80000,
                        "rsi": 35.0,
                        "adx": 20.0,
                        "volume": 1000000,
                        "change_percent": 1.5,
                        "change_rate": 1.5,
                        "market_cap": 480000000000000,
                        "market": "kr",
                    }
                ],
                "source": "tvscreener",
                "count": 1,
                "filters_applied": {},
                "error": None,
            }

        monkeypatch.setattr(
            screening_kr, "_screen_kr_via_tvscreener", mock_tvscreener_kr
        )

        # Mock capability check to allow tvscreener path
        monkeypatch.setattr(
            screening_kr,
            "_can_use_tvscreener_stock_path",
            lambda **kwargs: True,
        )
        monkeypatch.setattr(
            screening_kr,
            "_get_tvscreener_stock_capability_snapshot",
            AsyncMock(return_value=object()),
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="kr",
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )
        assert "results" in result

    @pytest.mark.asyncio
    async def test_us_sort_by_rsi_succeeds(self, monkeypatch):
        """Test US market allows sort_by='rsi' (tvscreener provides RSI)."""

        async def mock_tvscreener_us(*args, **kwargs):
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "code": "AAPL",
                        "name": "Apple Inc",
                        "price": 180.0,
                        "rsi": 42.0,
                        "adx": 25.0,
                        "volume": 50000000,
                        "change_percent": 0.8,
                        "change_rate": 0.8,
                        "market_cap": 2800000000000,
                        "market": "us",
                    }
                ],
                "source": "tvscreener",
                "count": 1,
                "filters_applied": {},
                "error": None,
            }

        monkeypatch.setattr(
            screening_us, "_screen_us_via_tvscreener", mock_tvscreener_us
        )
        monkeypatch.setattr(
            screening_us,
            "_can_use_tvscreener_stock_path",
            lambda **kwargs: True,
        )
        monkeypatch.setattr(
            screening_us,
            "_get_tvscreener_stock_capability_snapshot",
            AsyncMock(return_value=object()),
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="us",
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )
        assert "results" in result

    @pytest.mark.asyncio
    async def test_kr_sort_by_trade_amount_raises_error(
        self, mock_krx_stocks, monkeypatch
    ):
        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        with pytest.raises(
            ValueError, match=".*trade_amount.*only supported for crypto.*"
        ):
            await tools["screen_stocks"](
                market="kr",
                asset_type="stock",
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="trade_amount",
                sort_order="desc",
                limit=20,
            )

    @pytest.mark.asyncio
    async def test_crypto_enriches_metrics_without_explicit_rsi_filters(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )
        assert all("score" not in item for item in result["results"])

    @pytest.mark.asyncio
    async def test_screen_crypto_uses_batch_realtime_rsi_engine(
        self, mock_upbit_coins, monkeypatch
    ):
        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="asc",
            limit=20,
        )

        assert all("rsi_14" not in item for item in result["results"])

    @pytest.mark.asyncio
    async def test_crypto_sort_by_rsi_desc_forces_asc_with_warning(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:AKRW", "UPBIT:BKRW", "UPBIT:CKRW"],
                "name": ["AKRW", "BKRW", "CKRW"],
                "description": ["A coin", "B coin", "C coin"],
                "price": [1_000.0, 1_000.0, 1_000.0],
                "change_percent": [-0.01, -0.02, -0.03],
                "relative_strength_index_14": [24.0, 22.0, 27.0],
                "average_directional_index_14": [20.0, 20.0, 20.0],
                "value_traded": [100.0, 300.0, 1_000.0],
                "market_cap": [10.0, 20.0, 30.0],
                "exchange": ["UPBIT", "UPBIT", "UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, Any]]:
            return [
                {"market": code, "acc_trade_volume_24h": 1.0} for code in market_codes
            ]

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return set()

        async def mock_market_cap_cache_get() -> dict[str, Any]:
            return {
                "data": {},
                "cached": True,
                "age_seconds": 0.0,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=20,
        )

        assert result["filters_applied"]["sort_order"] == "asc"
        assert any("requested desc was ignored" in w for w in result["warnings"])
        assert [item["symbol"] for item in result["results"]] == [
            "KRW-B",
            "KRW-A",
            "KRW-C",
        ]

    @pytest.mark.asyncio
    async def test_crypto_market_cap_sort_uses_public_market_cap(
        self, fake_crypto_tvscreener_module, monkeypatch
    ):
        tv_service = AsyncMock()
        tv_service.query_crypto_screener.return_value = pd.DataFrame(
            {
                "symbol": ["UPBIT:BTCKRW", "UPBIT:ETHKRW", "UPBIT:XRPKRW"],
                "name": ["BTCKRW", "ETHKRW", "XRPKRW"],
                "description": ["BTC TV", "ETH TV", "XRP TV"],
                "price": [150_000_000.0, 5_000_000.0, 3_000.0],
                "change_percent": [1.0, 1.0, 1.0],
                "relative_strength_index_14": [45.5, 32.1, 68.9],
                "average_directional_index_14": [25.3, 18.7, 42.1],
                "value_traded": [9_000.0, 1_000.0, 2_000.0],
                "market_cap": [20.0, 10.0, 50.0],
                "exchange": ["UPBIT", "UPBIT", "UPBIT"],
            }
        )

        async def mock_fetch_multiple_tickers(
            market_codes: list[str],
        ) -> list[dict[str, Any]]:
            return [
                {"market": code, "acc_trade_volume_24h": 1.0} for code in market_codes
            ]

        async def mock_warning_markets(db=None, *, quote_currency: str) -> set[str]:
            assert quote_currency == "KRW"
            return set()

        async def mock_market_cap_cache_get() -> dict[str, Any]:
            return {
                "data": {
                    "ETH": {
                        "market_cap": 100.0,
                        "market_cap_rank": 2,
                    }
                },
                "cached": True,
                "age_seconds": 0.0,
                "stale": False,
                "error": None,
            }

        monkeypatch.setattr(
            screening_crypto,
            "_import_tvscreener",
            lambda: fake_crypto_tvscreener_module,
        )
        monkeypatch.setattr(
            screening_crypto,
            "TvScreenerService",
            lambda timeout=30.0: tv_service,
        )
        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_tickers",
            mock_fetch_multiple_tickers,
        )
        monkeypatch.setattr(
            screening_crypto,
            "get_upbit_warning_markets",
            mock_warning_markets,
        )
        monkeypatch.setattr(
            screening_crypto._CRYPTO_MARKET_CAP_CACHE,
            "get",
            mock_market_cap_cache_get,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=3,
        )

        assert tv_service.query_crypto_screener.await_args.kwargs["sort_by"] == (
            fake_crypto_tvscreener_module.CryptoField.MARKET_CAP
        )
        assert [item["symbol"] for item in result["results"]] == [
            "KRW-ETH",
            "KRW-XRP",
            "KRW-BTC",
        ]
        assert [item["market_cap"] for item in result["results"]] == [100.0, 50.0, 20.0]


class TestScreenStocksRsiLogging:
    """Test RSI enrichment logging and symbol selection behavior."""

    @pytest.mark.asyncio
    async def test_kr_rsi_uses_short_code_over_code(self, monkeypatch):
        """KR RSI enrichment should prefer short_code for KIS OHLCV lookup."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    }
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        called_symbols: list[tuple[str, str, int]] = []

        async def mock_fetch_ohlcv(symbol, market_type, count):
            import pandas as pd

            called_symbols.append((symbol, market_type, count))
            return pd.DataFrame({"close": [100.0 + i for i in range(50)]})

        def mock_calculate_rsi(close):
            return {"14": 42.0}

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.market_data_indicators._fetch_ohlcv_for_indicators",
            mock_fetch_ohlcv,
        )
        monkeypatch.setattr(
            "app.mcp_server.tooling.market_data_indicators._calculate_rsi",
            mock_calculate_rsi,
        )

        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert called_symbols, "OHLCV fetch should be called for RSI enrichment"
        assert called_symbols[0][0] == "005930"
        assert called_symbols[0][1] == "equity_kr"
        assert result["results"][0]["rsi"] == 42.0

    @pytest.mark.asyncio
    async def test_crypto_rsi_falls_back_to_market_field(self, monkeypatch, caplog):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        realtime_rsi_mock = AsyncMock(return_value={})

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(
            screening_crypto,
            "compute_crypto_realtime_rsi_map",
            realtime_rsi_mock,
        )

        caplog.set_level(logging.ERROR)
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="asc",
            limit=5,
        )

        assert result["returned_count"] == 1
        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 1
        assert diagnostics["failed"] == 1

    @pytest.mark.asyncio
    async def test_kr_rsi_ohlcv_exception_logs_error(self, monkeypatch, caplog):
        """KR RSI enrichment should log errors and return item without RSI on OHLCV failure."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    }
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        async def mock_fetch_ohlcv(symbol, market_type, count):
            raise RuntimeError("boom-kr")

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        caplog.set_level(logging.ERROR)
        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 1
        assert result["results"][0].get("rsi") is None
        assert any("[RSI-KR] ❌ Failed" in record.message for record in caplog.records)
        assert any("RuntimeError" in record.message for record in caplog.records)

    @pytest.mark.asyncio
    async def test_kr_rsi_empty_or_malformed_ohlcv_keeps_base_rows(self, monkeypatch):
        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    },
                    {
                        "code": "KR7000660001",
                        "short_code": "000660",
                        "name": "SK하이닉스",
                        "close": 150000.0,
                        "volume": 900,
                        "market_cap": 900_000,
                    },
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        async def mock_fetch_ohlcv(symbol, market_type, count):
            assert market_type == "equity_kr"
            assert count == 50
            if symbol == "005930":
                return pd.DataFrame()
            return pd.DataFrame(
                {
                    "date": pd.to_datetime(["2026-03-07"]),
                    "open": [1.0],
                }
            )

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 2
        assert [item["code"] for item in result["results"]] == [
            "KR7005930003",
            "KR7000660001",
        ]
        assert all(item.get("rsi") is None for item in result["results"])
        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 2
        assert diagnostics["succeeded"] == 0
        assert diagnostics["failed"] == 2
        assert diagnostics["error_samples"]
        assert diagnostics["error_samples"][0] == "Missing OHLCV close data"

    @pytest.mark.asyncio
    async def test_crypto_rsi_ohlcv_exception_logs_error(self, monkeypatch, caplog):
        """Crypto RSI enrichment should log errors and continue on OHLCV failure."""

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )

        caplog.set_level(logging.ERROR)
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 1
        assert result["results"][0].get("rsi") is None
        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["failed"] == 1
        assert diagnostics["error_samples"] == ["RuntimeError: boom-crypto"]

    @pytest.mark.asyncio
    async def test_kr_rsi_rate_limited_diagnostic_counts(self, monkeypatch):
        """KR RSI enrichment should surface rate-limited diagnostics."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return [
                    {
                        "code": "KR7005930003",
                        "short_code": "005930",
                        "name": "삼성전자",
                        "close": 80000.0,
                        "volume": 1000,
                        "market_cap": 1_000_000,
                    }
                ]
            return []

        async def mock_fetch_valuation_all_cached(market):
            return {}

        async def mock_fetch_ohlcv(symbol, market_type, count):
            raise RateLimitExceededError("KIS rate limit retries exhausted")

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )
        monkeypatch.setattr(
            screening_kr,
            "fetch_valuation_all_cached",
            mock_fetch_valuation_all_cached,
        )
        monkeypatch.setattr(
            screening_kr, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )

        result = await analysis_screen_core._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 1
        assert diagnostics["succeeded"] == 0
        assert diagnostics["rate_limited"] == 1
        assert diagnostics["failed"] == 0

    @pytest.mark.asyncio
    async def test_crypto_rsi_rate_limited_diagnostic_counts(self, monkeypatch):
        """Crypto RSI enrichment should surface rate-limited diagnostics."""

        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=70,
            sort_by="trade_amount",
            sort_order="desc",
            limit=5,
        )

        diagnostics = result["meta"]["rsi_enrichment"]
        assert diagnostics["attempted"] == 1
        assert diagnostics["succeeded"] == 0
        assert diagnostics["rate_limited"] == 1
        assert diagnostics["failed"] == 0

    @pytest.mark.asyncio
    async def test_crypto_rsi_gather_exception_is_logged(self, monkeypatch, caplog):
        async def mock_fetch_top_traded_coins(fiat):
            return [
                {
                    "market": "KRW-BTC",
                    "korean_name": "비트코인",
                    "trade_price": 100_000_000,
                    "signed_change_rate": 0.01,
                    "acc_trade_volume_24h": 123.0,
                    "acc_trade_price_24h": 456.0,
                }
            ]

        async def mock_gather(*aws, **kwargs):
            for awaitable in aws:
                awaitable.close()
            return [RuntimeError("forced gather failure")]

        monkeypatch.setattr(
            upbit_service, "fetch_top_traded_coins", mock_fetch_top_traded_coins
        )
        monkeypatch.setattr(screening_crypto.asyncio, "gather", mock_gather)

        caplog.set_level(logging.WARNING)
        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="rsi",
            sort_order="desc",
            limit=5,
        )

        assert result["returned_count"] == 1
        assert any(
            "parallel execution returned unexpected shape" in warning
            for warning in result.get("warnings", [])
        )


class TestScreenStocksFilters:
    """Test filter application."""

    @pytest.mark.asyncio
    async def test_kr_min_market_cap(self, mock_krx_stocks, monkeypatch):
        """Test KR market with minimum market cap filter."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=100000000000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 100000000000

    @pytest.mark.asyncio
    async def test_us_min_market_cap(self, mock_yfinance_screen, monkeypatch):
        """Test US market with minimum market cap filter."""

        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=1000000000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 1000000000
        assert "error" not in result, f"Unexpected error: {result.get('error')}"

    @pytest.mark.asyncio
    async def test_crypto_min_market_cap(self, mock_upbit_coins, monkeypatch):
        """Test crypto market with minimum market cap filter - not supported, warning added."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            min_market_cap=300000000000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 300000000000
        assert "warnings" in result
        assert any(
            "min_market_cap" in w and "not supported" in w for w in result["warnings"]
        )

    @pytest.mark.asyncio
    async def test_kr_min_market_cap_only_no_naver_queries(
        self, mock_krx_stocks, monkeypatch
    ):
        """Test KR market with min_market_cap only - Naver Finance not called, but RSI enrichment still runs."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        # Track whether Naver Finance valuation queries are called (they shouldn't be)
        naver_finance_called = False

        async def mock_fetch_valuation(code):
            nonlocal naver_finance_called
            naver_finance_called = True
            return {}

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        # RSI enrichment IS expected to run even with min_market_cap only
        # (policy: RSI auto-enrichment is maintained)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=100000,  # Only basic filter
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_market_cap"] == 100000
        # Verify Naver Finance was NOT called (uses KRX batch valuation instead)
        assert not naver_finance_called, (
            "Naver Finance should not be called for min_market_cap only"
        )


class TestScreenStocksSorting:
    """Test sorting functionality."""

    @pytest.mark.asyncio
    async def test_kr_sort_by_volume_desc(self, mock_krx_stocks, monkeypatch):
        """Test KR market sorted by volume descending."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["sort_by"] == "volume"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    async def test_us_sort_by_change_rate_asc(self, monkeypatch):
        """Test US market sorted by change rate ascending."""

        import yfinance as yf

        def mock_yfinance_screen_func(query, size, sortField, sortAsc, session=None):
            assert session is not None
            return {
                "quotes": [
                    {
                        "symbol": "AAPL",
                        "shortname": "Apple Inc.",
                        "lastprice": 175.5,
                        "percentchange": -1.0,
                        "dayvolume": 50000000,
                        "intradaymarketcap": 2800000000000,
                        "peratio": 28.5,
                        "forward_dividend_yield": 0.005,
                    },
                    {
                        "symbol": "MSFT",
                        "shortname": "Microsoft Corp",
                        "lastprice": 330.0,
                        "percentchange": 0.5,
                        "dayvolume": 20000000,
                        "intradaymarketcap": 2500000000000,
                        "peratio": 32.0,
                        "forward_dividend_yield": 0.008,
                    },
                ]
            }

        monkeypatch.setattr(yf, "screen", mock_yfinance_screen_func)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="asc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["sort_by"] == "change_rate"
        assert result["filters_applied"]["sort_order"] == "asc"
        assert "error" not in result, f"Unexpected error: {result.get('error')}"


class TestScreenStocksLimit:
    """Test limit parameter."""

    @pytest.mark.asyncio
    async def test_limit_enforcement(self, mock_krx_stocks, monkeypatch):
        """Test that limit parameter is properly enforced."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks * 5

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result is not None
        assert len(result["results"]) <= 5
        assert result["returned_count"] <= 5


class TestScreenStocksDividendYieldNormalization:
    """Test dividend yield input normalization (decimal vs percentage)."""

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_decimal_input(
        self, mock_krx_stocks, monkeypatch
    ):
        """Test KR market with decimal dividend yield input (0.03)."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=0.03,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_dividend_yield_input"] == 0.03
        assert result["filters_applied"]["min_dividend_yield_normalized"] == 0.03

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_percent_input(
        self, mock_krx_stocks, monkeypatch
    ):
        """Test KR market with percentage dividend yield input (3.0)."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=3.0,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_dividend_yield_input"] == 3.0
        assert result["filters_applied"]["min_dividend_yield_normalized"] == 0.03

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_normalization_one_percent_input(
        self, mock_krx_stocks, monkeypatch
    ):
        """Test KR market with 1.0 input interpreted as 1% (0.01)."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=1.0,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["min_dividend_yield_input"] == 1.0
        assert result["filters_applied"]["min_dividend_yield_normalized"] == 0.01

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_equivalence(self, mock_krx_stocks, monkeypatch):
        """Test that decimal (0.03) and percent (3.0) inputs produce identical results."""

        async def mock_fetch_stock_all_cached(market):
            stocks = mock_krx_stocks.copy()
            for stock in stocks:
                stock["dividend_yield"] = 0.03
            return stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result_decimal = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=0.03,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        result_percent = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=3.0,
            max_rsi=None,
            sort_by="dividend_yield",
            sort_order="desc",
            limit=20,
        )

        assert (
            result_decimal["filters_applied"]["min_dividend_yield_normalized"]
            == result_percent["filters_applied"]["min_dividend_yield_normalized"]
        )
        assert result_decimal["filters_applied"]["min_dividend_yield_input"] == 0.03
        assert result_percent["filters_applied"]["min_dividend_yield_input"] == 3.0

    @pytest.mark.asyncio
    async def test_kr_dividend_yield_none_input(self, mock_krx_stocks, monkeypatch):
        """Test KR market with None dividend yield input - no input/normalized keys."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert "min_dividend_yield_input" not in result["filters_applied"]
        assert "min_dividend_yield_normalized" not in result["filters_applied"]


class TestScreenStocksPhase2Spec:
    """Test Phase 2 specification compliance."""

    @pytest.mark.asyncio
    async def test_kr_etf_category_semiconductor(self, mock_krx_etfs, monkeypatch):
        """Test KR ETF category filtering with '반도체' category."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type=None,
            category="반도체",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert result is not None
        assert result["filters_applied"]["asset_type"] == "etf"
        assert result["filters_applied"]["category"] == "반도체"

        # Verify at least one result matches semiconductor category
        if len(result["results"]) > 0:
            semiconductor_found = False
            for item in result["results"]:
                assert item.get("asset_type") == "etf", "All results should be ETFs"
                if "category" in item and "반도체" in item["category"]:
                    semiconductor_found = True
                    break
            assert semiconductor_found, "Should find at least one semiconductor ETF"

    @pytest.mark.asyncio
    async def test_kr_etf_has_asset_type_and_category(self, mock_krx_etfs, monkeypatch):
        """Test KR ETF results have asset_type='etf' and category field."""

        async def mock_fetch_etf_all_cached():
            return mock_krx_etfs

        monkeypatch.setattr(
            screening_kr, "fetch_etf_all_cached", mock_fetch_etf_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="etf",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=20,
        )

        assert len(result["results"]) > 0, "Should have ETF results"

        for item in result["results"]:
            assert item.get("asset_type") == "etf", (
                "All ETFs should have asset_type='etf'"
            )
            assert "category" in item, "All ETFs should have category field"
            assert isinstance(item["category"], str), "Category should be a string"

    @pytest.mark.asyncio
    async def test_kr_market_cap_unit_100m_won(self, mock_krx_stocks, monkeypatch):
        """Test KR min_market_cap filter uses 억원 (100 million KRW) unit."""

        async def mock_screen_kr_via_tvscreener(**kwargs):
            assert kwargs["min_market_cap"] == 200000
            assert kwargs["sort_by"] == "market_cap"
            return {
                "stocks": [
                    {
                        "symbol": "005930",
                        "name": "Samsung Electronics Co., Ltd.",
                        "price": 80000.0,
                        "change_percent": 2.5,
                        "volume": 1000.0,
                        "market_cap": 4_800_000,
                        "rsi": 35.0,
                        "adx": 20.0,
                        "market": "KOSPI",
                    }
                ],
                "count": 1,
                "filters_applied": {
                    "min_market_cap": 200000,
                    "sort_by": "market_cap",
                    "sort_order": "desc",
                },
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.kr._screen_kr_via_tvscreener",
            mock_screen_kr_via_tvscreener,
        )

        tools = build_tools()

        # Filter by min_market_cap=200000 (200,000억원 = 20조원)
        # Should only return 삼성전자 (4,800,000억원)
        # SK하이닉스 (150,000억원) should be filtered out
        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=200000,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        assert result["filters_applied"]["min_market_cap"] == 200000
        assert result["total_count"] == 1, "Only 삼성전자 should pass filter"
        assert len(result["results"]) == 1
        assert result["results"][0]["code"] == "005930"
        assert result["results"][0]["name"] == "Samsung Electronics Co., Ltd."
        assert result["results"][0]["market_cap"] == 4800000

    @pytest.mark.asyncio
    async def test_us_early_return_filters_applied_complete(self, monkeypatch):
        """Test US market early-return includes all filters_applied fields."""

        def mock_screen_none(query, size, sortField, sortAsc, session=None):
            assert session is not None
            return None

        import yfinance as yf

        monkeypatch.setattr(yf, "screen", mock_screen_none)

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=1000000000,
            max_per=25.0,
            min_dividend_yield=0.02,
            max_rsi=70,
            sort_by="market_cap",
            sort_order="desc",
            limit=20,
        )

        # Verify all filter keys are present even on early return
        assert "min_market_cap" in result["filters_applied"]
        assert "max_per" in result["filters_applied"]
        assert "min_dividend_yield_normalized" in result["filters_applied"]
        assert "max_rsi" in result["filters_applied"]
        assert "sort_by" in result["filters_applied"]
        assert "sort_order" in result["filters_applied"]

    @pytest.mark.asyncio
    async def test_us_max_rsi_filter_applied(self, mock_yfinance_screen, monkeypatch):
        async def mock_screen_us_via_tvscreener(**kwargs):
            assert kwargs["max_rsi"] == 70
            assert kwargs["sort_by"] == "volume"
            return {
                "stocks": [
                    {
                        "symbol": "AAPL",
                        "name": "Apple Inc.",
                        "price": 180.0,
                        "change_percent": 1.0,
                        "volume": 1000.0,
                        "rsi": 65.0,
                    },
                    {
                        "symbol": "GOOGL",
                        "name": "Alphabet Inc.",
                        "price": 140.0,
                        "change_percent": 0.5,
                        "volume": 900.0,
                        "rsi": 60.0,
                    },
                ],
                "count": 2,
                "filters_applied": {"max_rsi": 70, "sort_by": "volume"},
                "source": "tvscreener",
                "error": None,
            }

        monkeypatch.setattr(
            "app.mcp_server.tooling.screening.us._screen_us_via_tvscreener",
            mock_screen_us_via_tvscreener,
        )

        tools = build_tools()

        # Request with max_rsi filter
        result = await tools["screen_stocks"](
            market="us",
            asset_type=None,
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=70,
            sort_by="volume",
            sort_order="desc",
            limit=5,
        )

        assert result["filters_applied"]["max_rsi"] == 70
        assert result["total_count"] >= result["returned_count"]
        assert result["total_count"] == 2
        assert result["returned_count"] == 2
        assert [item["code"] for item in result["results"]] == ["AAPL", "GOOGL"]
        assert result["returned_count"] <= 2  # AAPL and GOOGL should pass

    @pytest.mark.asyncio
    async def test_limit_zero_error(self):
        """Test limit=0 raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="limit|between 1 and 50"):
            await tools["screen_stocks"](
                market="kr",
                asset_type="stock",
                category=None,
                min_market_cap=None,
                max_per=None,
                min_dividend_yield=None,
                max_rsi=None,
                sort_by="volume",
                sort_order="desc",
                limit=0,
            )

    @pytest.mark.asyncio
    async def test_limit_over_50_capped(self, mock_krx_stocks, monkeypatch):
        """Test limit>50 is capped to 50 (not an error, by design)."""

        async def mock_fetch_stock_all_cached(market):
            return mock_krx_stocks

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()

        result = await tools["screen_stocks"](
            market="kr",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="desc",
            limit=100,
        )

        # Should not raise error, but cap to 50
        assert result is not None
        assert result["returned_count"] <= 50

    @pytest.mark.asyncio
    async def test_strategy_preset_with_case_insensitive_inputs(
        self, mock_krx_stocks, monkeypatch
    ):
        """Uppercase inputs should normalize and strategy presets should override sort."""

        async def mock_fetch_stock_all_cached(market):
            if market == "STK":
                return mock_krx_stocks
            return []

        monkeypatch.setattr(
            screening_kr, "fetch_stock_all_cached", mock_fetch_stock_all_cached
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="KOSPI",
            asset_type="STOCK",
            category=None,
            strategy="MOMENTUM",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="volume",
            sort_order="asc",
            limit=20,
        )

        assert result["market"] == "kospi"
        assert result["filters_applied"]["sort_by"] == "change_rate"
        assert result["filters_applied"]["sort_order"] == "desc"

    @pytest.mark.asyncio
    async def test_crypto_high_volume_strategy_defaults_to_trade_amount(
        self, mock_upbit_coins, monkeypatch
    ):
        """Crypto high_volume preset should resolve to trade_amount sorting by default."""

        async def mock_fetch_top_traded_coins(fiat):
            return mock_upbit_coins

        monkeypatch.setattr(
            upbit_service,
            "fetch_top_traded_coins",
            mock_fetch_top_traded_coins,
        )

        tools = build_tools()
        result = await tools["screen_stocks"](
            market="crypto",
            asset_type=None,
            category=None,
            strategy="high_volume",
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_order="asc",
            limit=20,
        )

        assert result["market"] == "crypto"
        assert result["filters_applied"]["sort_by"] == "trade_amount"
        assert result["filters_applied"]["sort_order"] == "desc"
