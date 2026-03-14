"""
Tests for MCP fundamentals/analysis tools.

This module contains tests for:
- analyze_stock tool
- get_valuation tool
- get_short_interest tool
- get_kimchi_premium tool
- get_funding_rate tool
- get_market_index tool
- get_sector_peers tool
- simulate_avg_cost tool
- get_crypto_profile tool
- get_investment_opinions tool
"""

import asyncio
import dataclasses
import json
from unittest.mock import AsyncMock

import httpx
import numpy as np
import pandas as pd
import pytest
import yfinance as yf

import app.services.brokers.upbit.client as upbit_service
from app.mcp_server.tooling import (
    analysis_analyze,
    analysis_screen_core,
    analysis_screening,
    analysis_tool_handlers,
    fundamentals_sources_coingecko,
    fundamentals_sources_indices,
    fundamentals_sources_naver,
    market_data_indicators,
    shared,
)
from app.services import market_data as market_data_service
from app.services import naver_finance
from tests._mcp_tooling_support import (
    _patch_httpx_async_client,
    _patch_runtime_attr,
    _patch_yf_ticker,
    build_tools,
)

# ---------------------------------------------------------------------------
# analyze_stock Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestAnalyzeStock:
    """Test analyze_stock tool."""

    async def test_analysis_screening_analyze_alias_delegates_to_analysis_analyze(
        self, monkeypatch
    ):
        called: dict[str, object] = {}

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            called["symbol"] = symbol
            called["market"] = market
            called["include_peers"] = include_peers
            return {"symbol": symbol, "source": "analysis-analyze"}

        monkeypatch.setattr(analysis_analyze, "analyze_stock_impl", fake_impl)

        result = await analysis_screening._analyze_stock_impl("005930", "kr", False)

        assert result["source"] == "analysis-analyze"
        assert called == {
            "symbol": "005930",
            "market": "kr",
            "include_peers": False,
        }

    async def test_analyze_stock_tool_uses_analysis_screening_alias(self, monkeypatch):
        tools = build_tools()

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            return {
                "symbol": symbol,
                "market_type": "equity_kr",
                "source": "shim-test",
                "include_peers": include_peers,
            }

        monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_impl)

        result = await tools["analyze_stock"]("005930", market="kr")

        assert result["source"] == "shim-test"

    async def test_analyze_stock_tool_uses_analysis_screening_symbol_normalizer(
        self, monkeypatch
    ):
        tools = build_tools()

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            return {
                "symbol": symbol,
                "market_type": "equity_kr",
                "source": "normalized-shim",
                "include_peers": include_peers,
            }

        monkeypatch.setattr(
            analysis_screening,
            "_normalize_symbol_input",
            lambda symbol, market: "000123",
        )
        monkeypatch.setattr(analysis_screening, "_analyze_stock_impl", fake_impl)

        result = await tools["analyze_stock"]("123", market="kr")

        assert result["symbol"] == "000123"
        assert result["source"] == "normalized-shim"

    async def test_patch_runtime_attr_updates_analysis_analyze_dependencies(
        self, monkeypatch
    ):
        async def fake_fetch(symbol, market_type, count):
            _ = symbol, market_type, count
            return pd.DataFrame()

        original = analysis_analyze._fetch_ohlcv_for_indicators
        _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", fake_fetch)

        assert analysis_analyze._fetch_ohlcv_for_indicators is fake_fetch
        assert analysis_analyze._fetch_ohlcv_for_indicators is not original

    async def test_recommendation_generation_kr(self, monkeypatch):
        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
            "indicators": {
                "indicators": {
                    "rsi": {"14": 45.0},
                    "bollinger": {"lower": 74000},
                }
            },
            "support_resistance": {
                "supports": [{"price": 73000}],
                "resistances": [{"price": 77000, "strength": "medium"}],
            },
            "opinions": {
                "consensus": {
                    "buy_count": 2,
                    "avg_target_price": 85000,
                    "current_price": 75000,
                },
            },
        }

        # Test _build_recommendation_for_equity directly
        recommendation = shared.build_recommendation_for_equity(
            mock_analysis, "equity_kr"
        )

        assert recommendation is not None
        rec = recommendation
        assert "action" in rec
        assert "confidence" in rec
        assert "buy_zones" in rec
        assert "sell_targets" in rec
        assert "stop_loss" in rec
        assert "reasoning" in rec

    async def test_recommendation_generation_skips_unavailable_consensus_counts(self):
        mock_analysis = {
            "symbol": "AAPL",
            "market_type": "equity_us",
            "source": "yahoo",
            "quote": {"price": 150.0},
            "support_resistance": {"supports": [], "resistances": []},
            "opinions": {
                "consensus": {
                    "buy_count": None,
                    "hold_count": None,
                    "sell_count": None,
                    "strong_buy_count": None,
                    "total_count": None,
                    "avg_target_price": None,
                    "max_target_price": None,
                }
            },
        }

        recommendation = shared.build_recommendation_for_equity(
            mock_analysis, "equity_us"
        )

        assert recommendation is not None
        assert recommendation["action"] == "hold"
        assert recommendation["confidence"] == "low"
        assert "Analyst consensus" not in recommendation["reasoning"]

    async def test_recommendation_generation_skips_partially_unavailable_consensus_counts(
        self,
    ):
        mock_analysis = {
            "symbol": "AAPL",
            "market_type": "equity_us",
            "source": "yahoo",
            "quote": {"price": 150.0},
            "support_resistance": {"supports": [], "resistances": []},
            "opinions": {
                "consensus": {
                    "buy_count": None,
                    "hold_count": 2,
                    "sell_count": 8,
                    "strong_buy_count": None,
                    "total_count": 10,
                    "avg_target_price": None,
                    "max_target_price": None,
                }
            },
        }

        recommendation = shared.build_recommendation_for_equity(
            mock_analysis, "equity_us"
        )

        assert recommendation is not None
        assert recommendation["action"] == "hold"
        assert recommendation["confidence"] == "low"
        assert "Analyst consensus" not in recommendation["reasoning"]

    async def test_build_recommendation_for_equity_exposes_rsi14(self):
        """Test that rsi14 value is exposed in recommendation payload."""
        analysis = {
            "quote": {"price": 150.0},
            "indicators": {"indicators": {"rsi": {"14": 45.8}}},
            "support_resistance": {"supports": [], "resistances": []},
        }
        rec = shared.build_recommendation_for_equity(analysis, "equity_us")
        assert rec is not None
        assert rec["rsi14"] == 45.8

    async def test_build_recommendation_for_equity_keeps_zero_rsi(self):
        """Test that rsi14=0.0 is NOT treated as missing."""
        analysis = {
            "quote": {"price": 150.0},
            "indicators": {"indicators": {"rsi": {"14": 0.0}}},
            "support_resistance": {"supports": [], "resistances": []},
        }
        rec = shared.build_recommendation_for_equity(analysis, "equity_us")
        assert rec is not None
        assert rec["rsi14"] == 0.0

    async def test_analyze_stock_us_includes_recommendation_rsi14(self, monkeypatch):
        """Test that rsi14 is surfaced in recommendation payload for US market."""
        tools = build_tools()

        async def mock_fetch_ohlcv(symbol, market_type, count):
            return pd.DataFrame(
                {
                    "date": ["2024-01-01"],
                    "open": [150.0],
                    "high": [155.0],
                    "low": [148.0],
                    "close": [150.0],
                    "volume": [1000000],
                }
            )

        async def mock_get_indicators(
            symbol, indicators, market=None, preloaded_df=None
        ):
            return {"indicators": {"rsi": {"14": 45.8}, "bollinger": {"lower": 145.0}}}

        async def mock_get_support_resistance(symbol, market=None, preloaded_df=None):
            return {"supports": [{"price": 140.0}], "resistances": [{"price": 160.0}]}

        async def mock_get_quote(symbol, market_type):
            return {"symbol": symbol, "price": 150.0, "instrument_type": "equity_us"}

        _patch_runtime_attr(
            monkeypatch, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv
        )
        _patch_runtime_attr(monkeypatch, "_get_indicators_impl", mock_get_indicators)
        _patch_runtime_attr(
            monkeypatch, "_get_support_resistance_impl", mock_get_support_resistance
        )
        _patch_runtime_attr(monkeypatch, "_get_quote_impl", mock_get_quote)

        result = await tools["analyze_stock"]("AAPL", market="us")

        assert result["recommendation"]["rsi14"] == 45.8

    async def test_recommendation_not_included_crypto(self, monkeypatch):
        tools = build_tools()

        mock_analysis = {
            "symbol": "KRW-BTC",
            "market_type": "crypto",
            "source": "upbit",
            "quote": {"current_price": 80000000},
            "indicators": {
                "rsi": 50.0,
                "bollinger_bands": {
                    "lower": 78000000,
                    "middle": 80000000,
                    "upper": 82000000,
                },
            },
            "support_resistance": {
                "supports": [{"price": 75000000}],
                "resistances": [{"price": 85000000}],
            },
        }

        _patch_runtime_attr(
            monkeypatch, "_analyze_stock_impl", lambda s, m, i: mock_analysis
        )

        result = await tools["analyze_stock"]("KRW-BTC", market="crypto")

        assert "recommendation" not in result

    async def test_us_opinions_schema_consistency(self, monkeypatch):
        """Test that US opinions have the 'opinions' key."""
        # Mock yfinance data
        mock_opinions = {
            "instrument_type": "equity_us",
            "source": "yfinance",
            "symbol": "AAPL",
            "count": 2,
            "opinions": [
                {
                    "firm": "Firm A",
                    "rating": "buy",
                    "date": "2024-01-01",
                    "target_price": 200,
                },
                {"firm": "Firm B", "rating": "hold", "date": "2024-01-02"},
            ],
            "consensus": {
                "buy_count": 1,
                "hold_count": 1,
                "sell_count": 0,
                "total_count": 2,
                "avg_target_price": 200,
                "current_price": 150,
            },
        }

        async def mock_fetch(symbol, limit):
            return mock_opinions

        _patch_runtime_attr(
            monkeypatch,
            "_fetch_investment_opinions_yfinance",
            mock_fetch,
        )

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        # Only opinions key should exist
        assert "opinions" in result
        assert len(result["opinions"]) == 2

    async def test_numeric_symbol_normalization_analyze_stock(self, monkeypatch):
        """Test that analyze_stock accepts numeric symbols and normalizes them."""
        tools = build_tools()

        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
        }

        _patch_runtime_attr(
            monkeypatch, "_analyze_stock_impl", lambda s, m, i: mock_analysis
        )

        # Test with integer input
        result = await tools["analyze_stock"](5930, market="kr")
        assert result["symbol"] == "005930"

        # Test with string input (should also work)
        result = await tools["analyze_stock"]("5930", market="kr")
        assert result["symbol"] == "005930"

    async def test_numeric_symbol_normalization_analyze_portfolio(self, monkeypatch):
        """Test that analyze_portfolio accepts numeric symbols and normalizes them."""
        tools = build_tools()

        def mock_impl(symbol, market, include_peers):
            return {
                "symbol": symbol,
                "market_type": "equity_kr",
                "source": "kis",
                "quote": {"price": 75000},
            }

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", mock_impl)

        # Test with mixed numeric and string symbols
        result = await tools["analyze_portfolio"](
            symbols=[12450, "005930"], market="kr"
        )

        assert "results" in result
        # Both symbols should be normalized to 6-digit strings
        assert "012450" in result["results"]
        assert "005930" in result["results"]


@pytest.mark.asyncio
class TestAnalyzeStockBatch:
    """Test analyze_stock_batch tool."""

    async def test_analyze_stock_batch_registration(self):
        """Test that analyze_stock_batch is registered as an MCP tool."""
        tools = build_tools()

        assert "analyze_stock_batch" in tools

    async def test_analyze_stock_batch_quick_summary(self, monkeypatch):
        """Test that analyze_stock_batch returns the compact summary contract."""
        tools = build_tools()

        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
            "indicators": {
                "indicators": {
                    "rsi": {"14": 45.0},
                    "bollinger": {"lower": 74000},
                }
            },
            "support_resistance": {
                "supports": [{"price": 73000}],
                "resistances": [{"price": 77000, "strength": "medium"}],
            },
            "opinions": {
                "consensus": {
                    "buy_count": 2,
                    "avg_target_price": 85000,
                    "current_price": 75000,
                }
            },
            "recommendation": {
                "action": "hold",
                "confidence": "low",
            },
            "news": [{"title": "Some news"}],
            "profile": {"description": "Company profile"},
        }

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            return mock_analysis

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", fake_impl)

        result = await tools["analyze_stock_batch"](["005930"], market="kr")

        assert result["summary"] == {
            "total_symbols": 1,
            "successful": 1,
            "failed": 0,
            "errors": [],
        }
        assert result["results"]["005930"] == {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "current_price": 75000,
            "rsi_14": 45.0,
            "consensus": {
                "buy_count": 2,
                "avg_target_price": 85000,
                "current_price": 75000,
            },
            "recommendation": {
                "action": "hold",
                "confidence": "low",
            },
            "supports": [{"price": 73000}],
            "resistances": [{"price": 77000, "strength": "medium"}],
        }

    async def test_analyze_stock_batch_quick_summary_ignores_non_sequence_levels(
        self, monkeypatch
    ):
        tools = build_tools()

        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
            "support_resistance": {
                "supports": {"price": 73000},
                "resistances": "77000",
            },
        }

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            return mock_analysis

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", fake_impl)

        result = await tools["analyze_stock_batch"](["005930"], market="kr")

        assert result["results"]["005930"]["supports"] == []
        assert result["results"]["005930"]["resistances"] == []

    async def test_analyze_stock_batch_quick_false_returns_full_payload(
        self, monkeypatch
    ):
        """Test that quick=False returns the unsummarized full analysis payload."""
        tools = build_tools()

        mock_analysis = {
            "symbol": "AAPL",
            "market_type": "equity_us",
            "source": "yahoo",
            "quote": {"price": 185.5},
            "news": [{"title": "Full payload should keep news"}],
            "profile": {"name": "Apple Inc."},
        }

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            return mock_analysis

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", fake_impl)

        result = await tools["analyze_stock_batch"](["AAPL"], market="us", quick=False)

        assert result["summary"] == {
            "total_symbols": 1,
            "successful": 1,
            "failed": 0,
            "errors": [],
        }
        assert result["results"]["AAPL"] == mock_analysis

    async def test_analyze_stock_batch_kr_symbol_normalization(self, monkeypatch):
        """Test that analyze_stock_batch normalizes numeric KR symbols."""
        tools = build_tools()

        def mock_impl(symbol: str, market: str | None, include_peers: bool):
            return {
                "symbol": symbol,
                "market_type": "equity_kr",
                "source": "kis",
                "quote": {"price": 75000},
            }

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", mock_impl)

        result = await tools["analyze_stock_batch"]([12450, "005930"], market="kr")

        assert "results" in result
        assert "012450" in result["results"]
        assert "005930" in result["results"]

    async def test_analyze_stock_batch_concurrency(self, monkeypatch):
        """Test that analyze_stock_batch executes symbol analysis concurrently."""
        tools = build_tools()
        call_tracker = {"active": 0, "max_active": 0}

        async def fake_impl(symbol: str, market: str | None, include_peers: bool):
            call_tracker["active"] += 1
            call_tracker["max_active"] = max(
                call_tracker["max_active"], call_tracker["active"]
            )
            await asyncio.sleep(0.01)
            call_tracker["active"] -= 1
            return {
                "symbol": symbol,
                "market_type": "equity_kr",
                "source": "kis",
                "quote": {"price": 75000},
            }

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", fake_impl)

        result = await tools["analyze_stock_batch"](
            ["005930", "000660", "035420"], market="kr"
        )

        assert "results" in result
        assert call_tracker["max_active"] > 1, "Expected concurrent execution"


@pytest.mark.asyncio
async def test_get_correlation_tool_uses_analysis_screening_correlation_alias(
    monkeypatch,
):
    tools = build_tools()

    async def fake_fetch(symbol, market_type, count):
        _ = symbol, market_type, count
        return pd.DataFrame({"close": [100.0, 101.0, 102.0, 103.0]})

    monkeypatch.setattr(
        analysis_screening,
        "_resolve_market_type",
        lambda symbol, market: ("equity_us", str(symbol).upper()),
    )
    monkeypatch.setattr(
        analysis_tool_handlers, "_fetch_ohlcv_for_indicators", fake_fetch
    )
    monkeypatch.setattr(
        analysis_screening,
        "_calculate_pearson_correlation",
        lambda left, right: 0.42,
    )

    result = await tools["get_correlation"](["aapl", "msft"], period=60)

    assert result["success"] is True
    assert result["symbols"] == ["AAPL", "MSFT"]
    assert result["correlation_matrix"][0][1] == 0.42


# ---------------------------------------------------------------------------
# get_valuation Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetValuation:
    """Test get_valuation tool."""

    async def test_successful_valuation_fetch(self, monkeypatch):
        """Test successful valuation fetch for Korean stock."""
        tools = build_tools()

        mock_valuation = {
            "symbol": "005930",
            "name": "삼성전자",
            "current_price": 75000,
            "per": 12.5,
            "pbr": 1.2,
            "roe": 18.5,
            "roe_controlling": 17.2,
            "dividend_yield": 0.02,
            "high_52w": 90000,
            "low_52w": 60000,
            "current_position_52w": 0.5,
        }

        async def mock_fetch_valuation(code):
            return mock_valuation

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        result = await tools["get_valuation"]("005930")

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert result["current_price"] == 75000
        assert result["per"] == 12.5
        assert result["pbr"] == 1.2
        assert result["roe"] == 18.5
        assert result["roe_controlling"] == 17.2
        assert result["dividend_yield"] == 0.02
        assert result["high_52w"] == 90000
        assert result["low_52w"] == 60000
        assert result["current_position_52w"] == 0.5
        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"

    async def test_successful_us_valuation_fetch(self, monkeypatch):
        """Test successful valuation fetch for US stock via yfinance."""
        tools = build_tools()

        mock_info = {
            "shortName": "Apple Inc.",
            "currentPrice": 185.5,
            "trailingPE": 28.5,
            "priceToBook": 45.2,
            "returnOnEquity": 1.473,
            "dividendYield": 0.005,
            "fiftyTwoWeekHigh": 199.62,
            "fiftyTwoWeekLow": 164.08,
        }

        class MockTicker:
            @property
            def info(self):
                return mock_info

        _patch_yf_ticker(monkeypatch, lambda s: MockTicker())

        result = await tools["get_valuation"]("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert result["current_price"] == 185.5
        assert result["per"] == 28.5
        assert result["pbr"] == 45.2
        assert result["roe"] == 147.3
        assert result["dividend_yield"] == 0.005
        assert result["high_52w"] == 199.62
        assert result["low_52w"] == 164.08
        assert result["current_position_52w"] == 0.6
        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "yfinance"

    async def test_us_valuation_with_explicit_market(self, monkeypatch):
        """Test US valuation with explicit market parameter."""
        tools = build_tools()

        mock_info = {
            "shortName": "NVIDIA Corp",
            "currentPrice": 500.0,
            "trailingPE": 60.0,
            "priceToBook": 30.0,
            "returnOnEquity": 0.85,
            "dividendYield": 0.001,
            "fiftyTwoWeekHigh": 550.0,
            "fiftyTwoWeekLow": 300.0,
        }

        class MockTicker:
            @property
            def info(self):
                return mock_info

        _patch_yf_ticker(monkeypatch, lambda s: MockTicker())

        result = await tools["get_valuation"]("NVDA", market="us")

        assert result["symbol"] == "NVDA"
        assert result["instrument_type"] == "equity_us"
        assert result["roe"] == 85.0

    async def test_rejects_crypto(self):
        """Test that crypto symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="cryptocurrencies"):
            await tools["get_valuation"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_valuation"]("")

    async def test_valuation_with_null_values(self, monkeypatch):
        """Test valuation response with some null values."""
        tools = build_tools()

        mock_valuation = {
            "symbol": "298040",
            "name": "효성중공업",
            "current_price": 450000,
            "per": None,
            "pbr": 2.1,
            "roe": None,
            "roe_controlling": None,
            "dividend_yield": 0.005,
            "high_52w": 500000,
            "low_52w": 200000,
            "current_position_52w": 0.83,
        }

        async def mock_fetch_valuation(code):
            return mock_valuation

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        result = await tools["get_valuation"]("298040")

        assert result["symbol"] == "298040"
        assert result["per"] is None
        assert result["roe"] is None
        assert result["current_position_52w"] == 0.83

    async def test_error_handling(self, monkeypatch):
        """Test error handling when fetch fails."""
        tools = build_tools()

        async def mock_fetch_valuation(code):
            raise Exception("Network error")

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        result = await tools["get_valuation"]("005930")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"

    async def test_us_error_handling(self, monkeypatch):
        """Test error handling when yfinance fetch fails."""
        tools = build_tools()

        class MockTicker:
            @property
            def info(self):
                raise Exception("API error")

        _patch_yf_ticker(monkeypatch, lambda s: MockTicker())

        result = await tools["get_valuation"]("AAPL")

        assert "error" in result
        assert result["source"] == "yfinance"
        assert result["symbol"] == "AAPL"
        assert result["instrument_type"] == "equity_us"

    async def test_invalid_market_raises_error(self):
        """Test that invalid market raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="must be 'us' or 'kr'"):
            await tools["get_valuation"]("AAPL", market="invalid")


# ---------------------------------------------------------------------------
# get_short_interest Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetShortInterest:
    """Test get_short_interest tool."""

    async def test_successful_short_interest_fetch(self, monkeypatch):
        """Test successful short interest fetch for Korean stock."""
        tools = build_tools()

        mock_short_interest = {
            "symbol": "005930",
            "name": "삼성전자",
            "short_data": [
                {
                    "date": "2024-01-15",
                    "short_amount": 1_000_000_000,
                    "total_amount": 20_000_000_000,
                    "short_ratio": 5.0,
                    "short_volume": None,
                    "total_volume": None,
                },
                {
                    "date": "2024-01-14",
                    "short_amount": 800_000_000,
                    "total_amount": 15_000_000_000,
                    "short_ratio": 5.33,
                    "short_volume": None,
                    "total_volume": None,
                },
            ],
            "avg_short_ratio": 5.17,
        }

        async def mock_fetch_short_interest(code, days):
            return mock_short_interest

        monkeypatch.setattr(
            market_data_service, "get_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert len(result["short_data"]) == 2
        assert result["short_data"][0]["date"] == "2024-01-15"
        assert result["short_data"][0]["short_amount"] == 1_000_000_000
        assert result["short_data"][0]["short_ratio"] == 5.0
        assert result["avg_short_ratio"] == 5.17
        assert "short_balance" not in result

    async def test_rejects_us_equity(self):
        """Test that US equity symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_short_interest"]("AAPL")

    async def test_rejects_crypto(self):
        """Test that crypto symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_short_interest"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_short_interest"]("")

    async def test_rejects_unpadded_kr_code(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_short_interest"]("5930")

    async def test_days_limit_capped(self, monkeypatch):
        """Test that days parameter is capped at 60."""
        tools = build_tools()

        captured_days = None

        async def mock_fetch_short_interest(code, days):
            nonlocal captured_days
            captured_days = days
            return {
                "symbol": code,
                "name": "테스트",
                "short_data": [],
                "avg_short_ratio": None,
            }

        monkeypatch.setattr(
            market_data_service, "get_short_interest", mock_fetch_short_interest
        )

        await tools["get_short_interest"]("005930", days=100)

        assert captured_days == 60

    async def test_error_handling(self, monkeypatch):
        """Test error handling when fetch fails."""
        tools = build_tools()

        async def mock_fetch_short_interest(code, days):
            raise RuntimeError("KIS API error")

        monkeypatch.setattr(
            market_data_service, "get_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("005930")

        assert "error" in result
        assert result["source"] == "kis"
        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"

    async def test_empty_short_data(self, monkeypatch):
        """Test response with no short data."""
        tools = build_tools()

        mock_short_interest = {
            "symbol": "000000",
            "name": "테스트종목",
            "short_data": [],
            "avg_short_ratio": None,
        }

        async def mock_fetch_short_interest(code, days):
            return mock_short_interest

        monkeypatch.setattr(
            market_data_service, "get_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("000000")

        assert result["symbol"] == "000000"
        assert result["short_data"] == []
        assert result["avg_short_ratio"] is None
        assert "short_balance" not in result


# ---------------------------------------------------------------------------
# get_kimchi_premium Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetKimchiPremium:
    """Test get_kimchi_premium tool."""

    def _patch_all(self, monkeypatch, upbit_prices, binance_resp, exchange_rate):
        """Helper to monkeypatch Upbit, Binance, and exchange rate."""

        async def mock_upbit(markets):
            return upbit_prices

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            mock_upbit,
        )

        class MockResponse:
            status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

            def __init__(self, data):
                self._data = data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "binance" in url:
                    return MockResponse(binance_resp)
                # exchange rate
                return MockResponse({"rates": {"KRW": exchange_rate}})

        _patch_httpx_async_client(monkeypatch, MockClient)

    async def test_single_symbol(self, monkeypatch):
        """Test kimchi premium for a single coin."""
        tools = build_tools()

        self._patch_all(
            monkeypatch,
            upbit_prices={"KRW-BTC": 150_000_000},
            binance_resp=[{"symbol": "BTCUSDT", "price": "102000.50"}],
            exchange_rate=1450.0,
        )

        result = await tools["get_kimchi_premium"]("BTC")

        assert result["source"] == "upbit+binance"
        assert result["exchange_rate"] == 1450.0
        assert result["count"] == 1
        item = result["data"][0]
        assert item["symbol"] == "BTC"
        assert item["upbit_krw"] == 150_000_000
        assert item["binance_usdt"] == 102000.50
        # (150_000_000 - 102000.50*1450) / (102000.50*1450) * 100
        expected_premium = round(
            (150_000_000 - 102000.50 * 1450) / (102000.50 * 1450) * 100, 2
        )
        assert item["premium_pct"] == expected_premium

    async def test_default_symbols(self, monkeypatch):
        """Test batch fetch when symbol is omitted."""
        tools = build_tools()

        _patch_runtime_attr(
            monkeypatch,
            "_resolve_batch_crypto_symbols",
            AsyncMock(return_value=["BTC", "ETH"]),
        )

        upbit = {"KRW-BTC": 150_000_000, "KRW-ETH": 4_500_000}
        binance = [
            {"symbol": "BTCUSDT", "price": "102000"},
            {"symbol": "ETHUSDT", "price": "3050"},
        ]

        self._patch_all(
            monkeypatch,
            upbit_prices=upbit,
            binance_resp=binance,
            exchange_rate=1450.0,
        )

        result = await tools["get_kimchi_premium"]()

        assert isinstance(result, list)
        assert len(result) == 2
        symbols = [d["symbol"] for d in result]
        assert symbols == ["BTC", "ETH"]
        assert result[0]["upbit_price"] == 150_000_000
        assert result[0]["binance_price"] == 102000.0
        assert "premium_pct" in result[0]

    async def test_strips_krw_prefix(self, monkeypatch):
        """Test that KRW- prefix is stripped from symbol."""
        tools = build_tools()

        self._patch_all(
            monkeypatch,
            upbit_prices={"KRW-ETH": 4_500_000},
            binance_resp=[{"symbol": "ETHUSDT", "price": "3050"}],
            exchange_rate=1450.0,
        )

        result = await tools["get_kimchi_premium"]("KRW-ETH")

        assert result["count"] == 1
        assert result["data"][0]["symbol"] == "ETH"

    async def test_error_handling(self, monkeypatch):
        """Test error handling when external API fails."""
        tools = build_tools()

        async def mock_upbit(markets):
            raise Exception("Upbit API down")

        monkeypatch.setattr(
            upbit_service,
            "fetch_multiple_current_prices",
            mock_upbit,
        )

        result = await tools["get_kimchi_premium"]("BTC")

        assert "error" in result
        assert result["source"] == "upbit+binance"
        assert result["instrument_type"] == "crypto"


# ---------------------------------------------------------------------------
# Funding Rate Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetFundingRate:
    """Test get_funding_rate tool."""

    def _patch_binance(self, monkeypatch, premium_resp, history_resp):
        """Helper to monkeypatch Binance futures API responses."""

        class MockResponse:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "premiumIndex" in url:
                    return MockResponse(premium_resp)
                return MockResponse(history_resp)

        _patch_httpx_async_client(monkeypatch, MockClient)

    async def test_successful_fetch(self, monkeypatch):
        """Test successful funding rate fetch for BTC."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 1707235200000,  # 2024-02-06T16:00:00Z
        }
        history = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0001",
                "fundingTime": 1707206400000,  # 2024-02-06T08:00:00Z
            },
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.00015",
                "fundingTime": 1707177600000,  # 2024-02-06T00:00:00Z
            },
        ]

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("BTC")

        assert result["symbol"] == "BTCUSDT"
        assert result["current_funding_rate"] == 0.0001
        assert result["current_funding_rate_pct"] == 0.01
        assert result["next_funding_time"] is not None
        assert len(result["funding_history"]) == 2
        assert result["funding_history"][0]["rate"] == 0.0001
        assert result["funding_history"][0]["rate_pct"] == 0.01
        assert result["avg_funding_rate_pct"] is not None
        assert "interpretation" in result

    async def test_strips_krw_prefix(self, monkeypatch):
        """Test that KRW- prefix is stripped from symbol."""
        tools = build_tools()

        premium = {
            "symbol": "ETHUSDT",
            "lastFundingRate": "0.0002",
            "nextFundingTime": 0,
        }
        history = []

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("KRW-ETH")

        assert result["symbol"] == "ETHUSDT"

    async def test_strips_usdt_suffix(self, monkeypatch):
        """Test that USDT suffix is stripped from symbol."""
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }
        history = []

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("BTCUSDT")

        assert result["symbol"] == "BTCUSDT"

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_funding_rate"]("")

    async def test_error_handling(self, monkeypatch):
        """Test error handling when Binance API fails."""
        tools = build_tools()

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                raise Exception("Binance API down")

        _patch_httpx_async_client(monkeypatch, MockClient)

        result = await tools["get_funding_rate"]("BTC")

        assert "error" in result
        assert result["source"] == "binance"
        assert result["symbol"] == "BTCUSDT"
        assert result["instrument_type"] == "crypto"

    async def test_batch_fetch_when_symbol_is_none(self, monkeypatch):
        tools = build_tools()

        _patch_runtime_attr(
            monkeypatch,
            "_resolve_batch_crypto_symbols",
            AsyncMock(return_value=["BTC", "ETH"]),
        )

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                assert "premiumIndex" in url
                return MockResponse(
                    [
                        {
                            "symbol": "BTCUSDT",
                            "lastFundingRate": "0.0001",
                            "nextFundingTime": 1707235200000,
                        },
                        {
                            "symbol": "ETHUSDT",
                            "lastFundingRate": "-0.0002",
                            "nextFundingTime": 1707235200000,
                        },
                        {
                            "symbol": "SOLUSDT",
                            "lastFundingRate": "0.0003",
                            "nextFundingTime": 1707235200000,
                        },
                    ]
                )

        _patch_httpx_async_client(monkeypatch, MockClient)

        result = await tools["get_funding_rate"]()

        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["symbol"] == "BTC"
        assert result[0]["funding_rate"] == 0.0001
        assert result[0]["next_funding_time"] is not None
        assert "interpretation" in result[0]

    async def test_limit_capped_at_100(self, monkeypatch):
        tools = build_tools()

        captured_params = {}

        class MockResponse:
            def __init__(self, data):
                self._data = data
                self.status_code = 200

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "fundingRate" in url and "premiumIndex" not in url:
                    captured_params.update(params or {})
                    return MockResponse([])
                return MockResponse(
                    {
                        "symbol": "BTCUSDT",
                        "lastFundingRate": "0.0001",
                        "nextFundingTime": 0,
                    }
                )

        _patch_httpx_async_client(monkeypatch, MockClient)

        await tools["get_funding_rate"]("BTC", limit=200)

        assert captured_params["limit"] == 100

    async def test_avg_funding_rate_calculation(self, monkeypatch):
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }
        history = [
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0002",
                "fundingTime": 1707206400000,
            },
            {
                "symbol": "BTCUSDT",
                "fundingRate": "0.0004",
                "fundingTime": 1707177600000,
            },
        ]

        self._patch_binance(monkeypatch, premium, history)

        result = await tools["get_funding_rate"]("BTC", limit=2)

        assert result["avg_funding_rate_pct"] == 0.03

    async def test_empty_history(self, monkeypatch):
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }

        self._patch_binance(monkeypatch, premium, [])

        result = await tools["get_funding_rate"]("BTC")

        assert result["funding_history"] == []
        assert result["avg_funding_rate_pct"] is None

    async def test_interpretation_present(self, monkeypatch):
        tools = build_tools()

        premium = {
            "symbol": "BTCUSDT",
            "lastFundingRate": "0.0001",
            "nextFundingTime": 0,
        }

        self._patch_binance(monkeypatch, premium, [])

        result = await tools["get_funding_rate"]("BTC")

        assert "interpretation" in result
        assert "positive" in result["interpretation"]
        assert "negative" in result["interpretation"]


# ---------------------------------------------------------------------------
# get_market_index Tool
# ---------------------------------------------------------------------------


def _naver_basic_json(
    close="2,450.50",
    change="-45.30",
    change_pct="-1.82",
    open_price="2,495.00",
    high="2,498.00",
    low="2,440.00",
    volume="450,000,000",
):
    return {
        "closePrice": close,
        "compareToPreviousClosePrice": change,
        "fluctuationsRatio": change_pct,
        "openPrice": open_price,
        "highPrice": high,
        "lowPrice": low,
        "accumulatedTradingVolume": volume,
    }


def _naver_price_history(n=3):
    items = []
    for i in range(n):
        items.append(
            {
                "localTradedAt": f"2026-02-0{i + 1}",
                "closePrice": f"{2400 + i * 10}",
                "openPrice": f"{2390 + i * 10}",
                "highPrice": f"{2420 + i * 10}",
                "lowPrice": f"{2380 + i * 10}",
                "accumulatedTradingVolume": f"{400_000_000 + i * 10_000_000}",
            }
        )
    return items


class _FakeResponse:
    """Fake httpx.Response for mocking."""

    def __init__(self, json_data, status_code=200):
        self._json_data = json_data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid")
            response = httpx.Response(
                status_code=self.status_code,
                request=request,
                json=self._json_data,
            )
            raise httpx.HTTPStatusError("error", request=request, response=response)

    def json(self):
        return self._json_data


@pytest.mark.asyncio
class TestGetMarketIndex:
    """Tests for get_market_index tool."""

    def _patch_naver(self, monkeypatch, basic_json, price_json):
        """Patch httpx.AsyncClient.get for naver API calls."""
        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            if "/basic" in url:
                return _FakeResponse(basic_json)
            elif "/price" in url:
                return _FakeResponse(price_json)
            raise ValueError(f"Unexpected URL: {url}")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

    def _patch_yfinance(self, monkeypatch, last_price=5500.0, prev_close=5450.0):
        """Patch yfinance for US index."""

        @dataclasses.dataclass(frozen=True)
        class MockFastInfo:
            last_price: float
            regular_market_previous_close: float
            open: float
            day_high: float
            day_low: float
            last_volume: int

        info = MockFastInfo(
            last_price=last_price,
            regular_market_previous_close=prev_close,
            open=5460.0,
            day_high=5510.0,
            day_low=5430.0,
            last_volume=3_500_000_000,
        )

        class MockTicker:
            fast_info = info

        def ticker_factory(symbol, session=None):
            assert session is not None
            return MockTicker()

        monkeypatch.setattr("yfinance.Ticker", ticker_factory)

    def _patch_yf_download(self, monkeypatch, rows=3):
        """Patch yf.download for US index history."""
        dates = pd.date_range("2026-02-01", periods=rows, freq="D")
        df = pd.DataFrame(
            {
                "Date": dates,
                "Open": [5460 + i * 10 for i in range(rows)],
                "High": [5510 + i * 10 for i in range(rows)],
                "Low": [5430 + i * 10 for i in range(rows)],
                "Close": [5500 + i * 10 for i in range(rows)],
                "Volume": [3_500_000_000 + i * 100_000 for i in range(rows)],
            }
        ).set_index("Date")

        def download_factory(*args, **kwargs):
            assert kwargs.get("session") is not None
            return df

        monkeypatch.setattr("yfinance.download", download_factory)

    async def test_single_kr_index(self, monkeypatch):
        """Test fetching a single Korean index (KOSPI)."""
        tools = build_tools()
        basic = _naver_basic_json()
        history = _naver_price_history(3)
        self._patch_naver(monkeypatch, basic, history)

        result = await tools["get_market_index"](symbol="KOSPI")

        assert "indices" in result
        assert len(result["indices"]) == 1
        idx = result["indices"][0]
        assert idx["symbol"] == "KOSPI"
        assert idx["name"] == "코스피"
        assert idx["current"] == 2450.50
        assert idx["change"] == -45.30
        assert idx["change_pct"] == -1.82
        assert idx["source"] == "naver"
        # open/high/low come from the first price record
        assert idx["open"] == 2390.0
        assert idx["high"] == 2420.0
        assert idx["low"] == 2380.0

        assert "history" in result
        assert len(result["history"]) == 3
        assert result["history"][0]["date"] == "2026-02-01"

    async def test_single_us_index(self, monkeypatch):
        """Test fetching a single US index (NASDAQ)."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch, last_price=17500.0, prev_close=17400.0)
        self._patch_yf_download(monkeypatch, rows=5)

        result = await tools["get_market_index"](symbol="NASDAQ")

        assert "indices" in result
        assert len(result["indices"]) == 1
        idx = result["indices"][0]
        assert idx["symbol"] == "NASDAQ"
        assert idx["name"] == "NASDAQ Composite"
        assert idx["current"] == 17500.0
        assert idx["change"] == 100.0
        assert idx["change_pct"] == pytest.approx(0.57, abs=0.01)
        assert idx["source"] == "yfinance"

        assert "history" in result
        assert len(result["history"]) == 5

    async def test_all_indices_no_symbol(self, monkeypatch):
        """Test fetching all major indices when no symbol specified."""
        tools = build_tools()

        # Patch both naver (for KOSPI, KOSDAQ) and yfinance (for SPX, NASDAQ)
        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            if "/basic" in url:
                return _FakeResponse(_naver_basic_json())
            elif "/price" in url:
                return _FakeResponse(_naver_price_history(1))
            raise ValueError(f"Unexpected URL: {url}")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)
        self._patch_yfinance(monkeypatch)

        result = await tools["get_market_index"]()

        assert "indices" in result
        assert len(result["indices"]) == 4
        assert "history" not in result

        # Verify we got both Korean and US indices
        symbols = [idx.get("symbol") for idx in result["indices"]]
        assert "KOSPI" in symbols
        assert "KOSDAQ" in symbols

    async def test_alias_sp500(self, monkeypatch):
        """Test SP500 alias resolves to same as SPX."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        self._patch_yf_download(monkeypatch)

        result = await tools["get_market_index"](symbol="SP500")

        assert result["indices"][0]["symbol"] == "SP500"
        assert result["indices"][0]["name"] == "S&P 500"

    async def test_alias_dow(self, monkeypatch):
        """Test DOW alias resolves to same as DJI."""
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        self._patch_yf_download(monkeypatch)

        result = await tools["get_market_index"](symbol="DOW")

        assert result["indices"][0]["symbol"] == "DOW"
        assert result["indices"][0]["name"] == "다우존스"

    async def test_unknown_symbol_raises_error(self):
        """Test that unknown index symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Unknown index symbol"):
            await tools["get_market_index"](symbol="UNKNOWN")

    async def test_invalid_period_raises_error(self):
        """Test that invalid period raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="period must be"):
            await tools["get_market_index"](symbol="KOSPI", period="hour")

    async def test_case_insensitive_symbol(self, monkeypatch):
        """Test that symbol is case-insensitive."""
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(2))

        result = await tools["get_market_index"](symbol="kospi")

        assert result["indices"][0]["symbol"] == "KOSPI"

    async def test_count_capped_at_100(self, monkeypatch):
        tools = build_tools()
        history_items = _naver_price_history(3)
        self._patch_naver(monkeypatch, _naver_basic_json(), history_items)

        result = await tools["get_market_index"](symbol="KOSPI", count=500)

        assert "indices" in result

    async def test_count_minimum_1(self, monkeypatch):
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(1))

        result = await tools["get_market_index"](symbol="KOSPI", count=-5)

        assert "indices" in result

    async def test_period_week(self, monkeypatch):
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(2))

        result = await tools["get_market_index"](symbol="KOSDAQ", period="week")

        assert "history" in result

    async def test_period_month(self, monkeypatch):
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        self._patch_yf_download(monkeypatch, rows=3)

        result = await tools["get_market_index"](symbol="SPX", period="month")

        assert "history" in result

    async def test_error_returns_error_payload(self, monkeypatch):
        tools = build_tools()

        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            raise RuntimeError("naver API down")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)

        result = await tools["get_market_index"](symbol="KOSPI")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "KOSPI"

    async def test_all_indices_partial_failure(self, monkeypatch):
        tools = build_tools()

        import httpx as _httpx

        async def fake_get(self_cli, url, **kwargs):
            raise RuntimeError("naver down")

        monkeypatch.setattr(_httpx.AsyncClient, "get", fake_get)
        self._patch_yfinance(monkeypatch)

        result = await tools["get_market_index"]()

        assert len(result["indices"]) == 4
        kr_results = [
            idx for idx in result["indices"] if idx.get("symbol") in ("KOSPI", "KOSDAQ")
        ]
        for kr in kr_results:
            assert "error" in kr

    async def test_us_history_empty_df(self, monkeypatch):
        tools = build_tools()
        self._patch_yfinance(monkeypatch)
        monkeypatch.setattr("yfinance.download", lambda *a, **kw: pd.DataFrame())

        result = await tools["get_market_index"](symbol="DJI")

        assert result["history"] == []

    async def test_strip_whitespace_symbol(self, monkeypatch):
        tools = build_tools()
        self._patch_naver(monkeypatch, _naver_basic_json(), _naver_price_history(2))

        result = await tools["get_market_index"](symbol="  KOSPI  ")

        assert result["indices"][0]["symbol"] == "KOSPI"


# ---------------------------------------------------------------------------
# get_sector_peers Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetSectorPeers:
    async def test_raises_on_empty_symbol(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_sector_peers"]("")

    async def test_raises_on_crypto_symbol(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="not available for cryptocurrencies"):
            await tools["get_sector_peers"]("KRW-BTC")

    async def test_raises_on_invalid_market(self):
        tools = build_tools()
        with pytest.raises(ValueError, match="market must be"):
            await tools["get_sector_peers"]("005930", market="invalid")

    async def test_korean_equity_success(self, monkeypatch):
        tools = build_tools()

        mock_data = {
            "symbol": "298040",
            "name": "효성중공업",
            "sector": "전기장비",
            "industry_code": 306,
            "current_price": 2195000,
            "change_pct": -5.96,
            "per": 46.93,
            "pbr": 9.36,
            "market_cap": 204581_0000_0000,
            "peers": [
                {
                    "symbol": "267260",
                    "name": "HD현대일렉트릭",
                    "current_price": 833000,
                    "change_pct": -4.58,
                    "per": 48.68,
                    "pbr": 16.86,
                    "market_cap": 300272_6300_0000,
                },
                {
                    "symbol": "010120",
                    "name": "LS ELECTRIC",
                    "current_price": 585000,
                    "change_pct": -5.49,
                    "per": 35.0,
                    "pbr": 5.2,
                    "market_cap": 175500_0000_0000,
                },
            ],
        }
        mock_fetch = AsyncMock(return_value=mock_data)
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        result = await tools["get_sector_peers"]("298040")

        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"
        assert result["symbol"] == "298040"
        assert result["name"] == "효성중공업"
        assert result["sector"] == "전기장비"
        assert len(result["peers"]) == 2
        assert result["peers"][0]["symbol"] == "267260"

        comp = result["comparison"]
        assert comp["avg_per"] is not None
        assert comp["avg_pbr"] is not None
        assert comp["target_per_rank"] is not None
        assert comp["target_pbr_rank"] is not None

    async def test_korean_equity_error_returns_payload(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(side_effect=RuntimeError("naver down"))
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        result = await tools["get_sector_peers"]("298040")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "298040"
        assert result["instrument_type"] == "equity_kr"

    async def test_us_equity_success(self, monkeypatch):
        tools = build_tools()

        # Mock Finnhub client
        class MockFinnhubClient:
            def company_peers(self, symbol):
                return ["MSFT", "GOOGL", "META"]

        _patch_runtime_attr(
            monkeypatch, "_get_finnhub_client", lambda: MockFinnhubClient()
        )

        # Mock yfinance
        _yf_data = {
            "AAPL": {
                "shortName": "Apple Inc.",
                "currentPrice": 180,
                "previousClose": 178,
                "trailingPE": 30,
                "priceToBook": 45,
                "marketCap": 3_000_000_000_000,
                "sector": "Technology",
                "industry": "Consumer Electronics",
            },
            "MSFT": {
                "shortName": "Microsoft",
                "currentPrice": 400,
                "previousClose": 398,
                "trailingPE": 35,
                "priceToBook": 12,
                "marketCap": 3_100_000_000_000,
                "sector": "Technology",
                "industry": "Software",
            },
            "GOOGL": {
                "shortName": "Alphabet",
                "currentPrice": 150,
                "previousClose": 149,
                "trailingPE": 25,
                "priceToBook": 6,
                "marketCap": 2_000_000_000_000,
                "sector": "Technology",
                "industry": "Internet",
            },
            "META": {
                "shortName": "Meta Platforms",
                "currentPrice": 500,
                "previousClose": 495,
                "trailingPE": 28,
                "priceToBook": 8,
                "marketCap": 1_300_000_000_000,
                "sector": "Technology",
                "industry": "Internet",
            },
        }

        class MockTicker:
            def __init__(self, ticker, session=None):
                assert session is not None
                self._ticker = ticker

            @property
            def info(self):
                return _yf_data.get(self._ticker, {})

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await tools["get_sector_peers"]("AAPL")

        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "finnhub+yfinance"
        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc."
        assert result["sector"] == "Technology"
        assert len(result["peers"]) == 3
        # Sorted by market_cap desc
        assert result["peers"][0]["symbol"] == "MSFT"

        comp = result["comparison"]
        assert comp["avg_per"] is not None
        assert comp["avg_pbr"] is not None

    async def test_auto_detects_korean_market(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(
            return_value={
                "symbol": "005930",
                "name": "삼성전자",
                "sector": "반도체",
                "industry_code": 278,
                "current_price": 80000,
                "change_pct": -1.0,
                "per": 20.0,
                "pbr": 1.5,
                "market_cap": 500_0000_0000_0000,
                "peers": [],
            }
        )
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        result = await tools["get_sector_peers"]("005930")

        assert result["instrument_type"] == "equity_kr"
        mock_fetch.assert_awaited_once_with("005930", limit=5)

    async def test_us_equity_error_returns_payload(self, monkeypatch):
        tools = build_tools()

        def raise_err():
            raise RuntimeError("finnhub down")

        _patch_runtime_attr(
            monkeypatch,
            "_get_finnhub_client",
            lambda: type(
                "C", (), {"company_peers": lambda self, symbol: raise_err()}
            )(),
        )

        result = await tools["get_sector_peers"]("AAPL")

        assert "error" in result
        assert result["source"] == "finnhub+yfinance"

    async def test_limit_capped_at_20(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(
            return_value={
                "symbol": "005930",
                "name": "삼성전자",
                "sector": "반도체",
                "industry_code": 278,
                "current_price": 80000,
                "change_pct": -1.0,
                "per": 20.0,
                "pbr": 1.5,
                "market_cap": 500_0000_0000_0000,
                "peers": [],
            }
        )
        monkeypatch.setattr(naver_finance, "fetch_sector_peers", mock_fetch)

        await tools["get_sector_peers"]("005930", limit=50)

        mock_fetch.assert_awaited_once_with("005930", limit=20)

    async def test_comparison_ranking_correct(self, monkeypatch):
        tools = build_tools()

        mock_data = {
            "symbol": "298040",
            "name": "효성중공업",
            "sector": "전기장비",
            "industry_code": 306,
            "current_price": 2195000,
            "change_pct": -5.96,
            "per": 20.0,
            "pbr": 5.0,
            "market_cap": 200000_0000_0000,
            "peers": [
                {
                    "symbol": "A",
                    "name": "Peer A",
                    "current_price": 100000,
                    "change_pct": 1.0,
                    "per": 30.0,
                    "pbr": 3.0,
                    "market_cap": 300000_0000_0000,
                },
                {
                    "symbol": "B",
                    "name": "Peer B",
                    "current_price": 200000,
                    "change_pct": -1.0,
                    "per": 40.0,
                    "pbr": 10.0,
                    "market_cap": 100000_0000_0000,
                },
            ],
        }
        monkeypatch.setattr(
            naver_finance,
            "fetch_sector_peers",
            AsyncMock(return_value=mock_data),
        )

        result = await tools["get_sector_peers"]("298040")
        comp = result["comparison"]

        assert comp["target_per_rank"] == "1/3"
        assert comp["target_pbr_rank"] == "2/3"
        assert comp["avg_per"] == 30.0
        assert comp["avg_pbr"] == 6.0


@pytest.mark.asyncio
async def test_sector_peers_us_dedupes_before_network_call(monkeypatch):
    tools = build_tools()
    yf_info_calls = []

    class MockFinnhubClient:
        def company_peers(self, symbol):
            return ["AAPL", "BRK.B", "BRK.A", "MSFT"]

    monkeypatch.setattr(
        fundamentals_sources_naver,
        "_get_finnhub_client",
        MockFinnhubClient,
    )

    def mock_yf_ticker(symbol, session=None):
        class MockTicker:
            @property
            def info(self):
                yf_info_calls.append(symbol)
                return {
                    "shortName": f"{symbol} Inc",
                    "currentPrice": 100.0,
                    "previousClose": 99.0,
                    "trailingPE": 15.0,
                    "priceToBook": 2.0,
                    "marketCap": 1000000000,
                    "industry": "Tech",
                }

        return MockTicker()

    monkeypatch.setattr(fundamentals_sources_naver.yf, "Ticker", mock_yf_ticker)

    await tools["get_sector_peers"]("TEST", market="us", limit=5)

    assert len(yf_info_calls) <= 4
    assert "BRK.B" not in yf_info_calls or "BRK.A" not in yf_info_calls


# ---------------------------------------------------------------------------
# get_crypto_profile Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetCryptoProfile:
    def _reset_cache(self):
        fundamentals_sources_coingecko._COINGECKO_LIST_CACHE["expires_at"] = 0.0
        fundamentals_sources_coingecko._COINGECKO_LIST_CACHE["symbol_to_ids"] = {}
        fundamentals_sources_coingecko._COINGECKO_PROFILE_CACHE.clear()

    async def test_get_crypto_profile_success_and_cache(self, monkeypatch):
        tools = build_tools()
        self._reset_cache()

        detail_calls = {"count": 0}

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/bitcoin" in url:
                    detail_calls["count"] += 1
                    return MockResponse(
                        {
                            "name": "Bitcoin",
                            "symbol": "btc",
                            "market_cap_rank": 1,
                            "categories": ["Store of Value"],
                            "description": {
                                "ko": "<p>비트코인은 대표적인 암호화폐입니다.</p>"
                            },
                            "market_data": {
                                "market_cap": {"krw": 2_000_000_000_000_000},
                                "total_volume": {"krw": 50_000_000_000_000},
                                "circulating_supply": 19_800_000,
                                "total_supply": 21_000_000,
                                "max_supply": 21_000_000,
                                "ath": {"krw": 140_000_000},
                                "ath_change_percentage": {"krw": -15.1},
                                "price_change_percentage_7d_in_currency": {"krw": 2.5},
                                "price_change_percentage_30d_in_currency": {"krw": 8.2},
                            },
                        }
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)

        result_first = await tools["get_crypto_profile"]("KRW-BTC")
        result_second = await tools["get_crypto_profile"]("BTC")

        assert result_first["name"] == "Bitcoin"
        assert result_first["symbol"] == "BTC"
        assert result_first["market_cap"] == 2_000_000_000_000_000
        assert result_first["market_cap_rank"] == 1
        assert result_first["total_volume_24h"] == 50_000_000_000_000
        assert result_first["ath"] == 140_000_000
        assert result_first["price_change_percentage_7d"] == 2.5
        assert "<" not in (result_first["description"] or "")
        assert result_second["symbol"] == "BTC"
        assert detail_calls["count"] == 1

    async def test_get_crypto_profile_unknown_symbol_returns_error(self, monkeypatch):
        tools = build_tools()
        self._reset_cache()

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    return MockResponse(
                        [{"id": "bitcoin", "symbol": "btc", "name": "Bitcoin"}]
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)

        result = await tools["get_crypto_profile"]("ZZZ")

        assert "error" in result
        assert result["source"] == "coingecko"
        assert result["symbol"] == "ZZZ"

    async def test_get_crypto_profile_uses_redis_cached_coin_list(self, monkeypatch):
        tools = build_tools()
        self._reset_cache()

        class FakeRedis:
            async def get(self, key):
                if key == "coingecko:coins:list:v1":
                    return json.dumps({"etc": ["ethereum-classic"]})
                return None

        monkeypatch.setattr(
            fundamentals_sources_coingecko,
            "_get_redis_client",
            AsyncMock(return_value=FakeRedis()),
        )

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    raise AssertionError("coins/list should not be called on redis hit")
                if "/coins/ethereum-classic" in url:
                    return MockResponse(
                        {
                            "name": "Ethereum Classic",
                            "symbol": "etc",
                            "market_data": {},
                        }
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)
        result = await tools["get_crypto_profile"]("ETC")

        assert result["symbol"] == "ETC"

    async def test_get_crypto_profile_writes_coin_list_to_redis_on_miss(
        self, monkeypatch
    ):
        tools = build_tools()
        self._reset_cache()

        class FakeRedis:
            def __init__(self):
                self.setex_calls: list[tuple[str, int, str]] = []

            async def get(self, key):
                return None

            async def setex(self, key, ttl, payload):
                self.setex_calls.append((key, ttl, payload))

        fake_redis = FakeRedis()
        monkeypatch.setattr(
            fundamentals_sources_coingecko,
            "_get_redis_client",
            AsyncMock(return_value=fake_redis),
        )

        list_calls = {"count": 0}

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    list_calls["count"] += 1
                    return MockResponse(
                        [
                            {
                                "id": "ethereum-classic",
                                "symbol": "etc",
                                "name": "Ethereum Classic",
                            }
                        ]
                    )
                if "/coins/ethereum-classic" in url:
                    return MockResponse(
                        {
                            "name": "Ethereum Classic",
                            "symbol": "etc",
                            "market_data": {},
                        }
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)
        result = await tools["get_crypto_profile"]("ETC")

        assert result["symbol"] == "ETC"
        assert list_calls["count"] == 1
        assert fake_redis.setex_calls[0][0] == "coingecko:coins:list:v1"
        assert fake_redis.setex_calls[0][1] == 86400

    async def test_get_crypto_profile_ignores_invalid_redis_payload_and_refetches(
        self, monkeypatch
    ):
        tools = build_tools()
        self._reset_cache()

        class FakeRedis:
            def __init__(self):
                self.setex_calls: list[tuple[str, int, str]] = []

            async def get(self, key):
                if key == "coingecko:coins:list:v1":
                    return json.dumps({"etc": "ethereum-classic"})
                return None

            async def setex(self, key, ttl, payload):
                self.setex_calls.append((key, ttl, payload))

        fake_redis = FakeRedis()
        monkeypatch.setattr(
            fundamentals_sources_coingecko,
            "_get_redis_client",
            AsyncMock(return_value=fake_redis),
        )

        list_calls = {"count": 0}

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    list_calls["count"] += 1
                    return MockResponse(
                        [
                            {
                                "id": "ethereum-classic",
                                "symbol": "etc",
                                "name": "Ethereum Classic",
                            }
                        ]
                    )
                if "/coins/ethereum-classic" in url:
                    return MockResponse(
                        {
                            "name": "Ethereum Classic",
                            "symbol": "etc",
                            "market_data": {},
                        }
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)
        result = await tools["get_crypto_profile"]("ETC")

        assert result["symbol"] == "ETC"
        assert list_calls["count"] == 1
        assert fake_redis.setex_calls[0][0] == "coingecko:coins:list:v1"

    async def test_get_crypto_profile_falls_back_when_redis_errors(self, monkeypatch):
        tools = build_tools()
        self._reset_cache()

        class BrokenRedis:
            async def get(self, key):
                raise RuntimeError("redis unavailable")

            async def setex(self, key, ttl, payload):
                raise RuntimeError("redis unavailable")

        monkeypatch.setattr(
            fundamentals_sources_coingecko,
            "_get_redis_client",
            AsyncMock(return_value=BrokenRedis()),
        )

        list_calls = {"count": 0}

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    list_calls["count"] += 1
                    return MockResponse(
                        [
                            {
                                "id": "ethereum-classic",
                                "symbol": "etc",
                                "name": "Ethereum Classic",
                            }
                        ]
                    )
                if "/coins/ethereum-classic" in url:
                    return MockResponse(
                        {
                            "name": "Ethereum Classic",
                            "symbol": "etc",
                            "market_data": {},
                        }
                    )
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)
        result = await tools["get_crypto_profile"]("ETC")

        assert "error" not in result
        assert result["name"] == "Ethereum Classic"
        assert result["symbol"] == "ETC"
        assert list_calls["count"] == 1

    async def test_get_crypto_profile_does_not_cache_invalid_coin_list_response(
        self, monkeypatch
    ):
        tools = build_tools()
        self._reset_cache()

        class FakeRedis:
            def __init__(self):
                self.setex_calls: list[tuple[str, int, str]] = []

            async def get(self, key):
                return None

            async def setex(self, key, ttl, payload):
                self.setex_calls.append((key, ttl, payload))

        fake_redis = FakeRedis()
        monkeypatch.setattr(
            fundamentals_sources_coingecko,
            "_get_redis_client",
            AsyncMock(return_value=fake_redis),
        )

        class MockResponse:
            status_code = 200

            def __init__(self, data):
                self._data = data

            def raise_for_status(self):
                pass

            def json(self):
                return self._data

        class MockClient:
            def __init__(self, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            async def get(self, url, params=None, **kw):
                if "/coins/list" in url:
                    return MockResponse({"unexpected": "format"})
                raise AssertionError(f"Unexpected URL: {url}")

        _patch_httpx_async_client(monkeypatch, MockClient)
        result = await tools["get_crypto_profile"]("ETC")

        assert "error" in result
        assert fake_redis.setex_calls == []


# ---------------------------------------------------------------------------
# get_investment_opinions Tool
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestGetInvestmentOpinions:
    """Test get_investment_opinions tool."""

    async def test_kr_symbol_int_with_leading_zeros(self, monkeypatch):
        """Test that integer symbol with leading zeros is restored for KR market."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "012450",
            "count": 1,
            "recommendations": [
                {
                    "firm": "Test Firm",
                    "rating": "buy",
                    "target_price": 50000,
                    "date": "2024-01-15",
                }
            ],
            "consensus": {
                "buy_count": 1,
                "hold_count": 0,
                "sell_count": 0,
                "total_count": 1,
                "avg_target_price": 50000,
                "median_target_price": 50000,
                "min_target_price": 50000,
                "max_target_price": 50000,
                "upside_pct": 50.0,
                "current_price": 33333,
            },
        }

        async def mock_fetch(code, limit):
            return {
                "instrument_type": "equity_kr",
                "source": "naver",
                **mock_opinions,
            }

        _patch_runtime_attr(monkeypatch, "_fetch_investment_opinions_naver", mock_fetch)

        # Pass integer 12450, should be normalized to "012450"
        result = await tools["get_investment_opinions"](12450, market="kr")

        assert result["symbol"] == "012450"
        assert result["count"] == 1
        assert "consensus" in result

    async def test_kr_symbol_int_auto_detect_market(self, monkeypatch):
        """Test that integer symbol is auto-detected as KR and normalized with zfill."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "005930",
            "count": 2,
            "recommendations": [
                {
                    "firm": "Firm A",
                    "rating": "buy",
                    "target_price": 85000,
                    "date": "2024-01-15",
                },
                {
                    "firm": "Firm B",
                    "rating": "hold",
                    "target_price": 82000,
                    "date": "2024-01-14",
                },
            ],
            "consensus": {
                "buy_count": 1,
                "hold_count": 1,
                "sell_count": 0,
                "total_count": 2,
                "avg_target_price": 83500,
                "current_price": 75000,
                "upside_pct": 11.33,
            },
        }

        async def mock_fetch(code, limit):
            return {
                "instrument_type": "equity_kr",
                "source": "naver",
                **mock_opinions,
            }

        _patch_runtime_attr(monkeypatch, "_fetch_investment_opinions_naver", mock_fetch)

        # Pass integer 5930, should be normalized to "005930"
        result = await tools["get_investment_opinions"](5930)

        assert result["symbol"] == "005930"

    async def test_us_market_with_only_targets(self, monkeypatch):
        """Test US market consensus calculation with only analyst_price_targets."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "AAPL",
            "count": 0,
            "recommendations": [],
            "consensus": {
                "buy_count": 0,
                "hold_count": 0,
                "sell_count": 0,
                "total_count": 0,
                "avg_target_price": 195.5,
                "median_target_price": 195.0,
                "min_target_price": 180.0,
                "max_target_price": 210.0,
                "upside_pct": 5.4,
                "current_price": 185.5,
            },
        }

        async def mock_fetch_yf(symbol, limit):
            return {
                "instrument_type": "equity_us",
                "source": "yfinance",
                **mock_opinions,
            }

        _patch_runtime_attr(
            monkeypatch, "_fetch_investment_opinions_yfinance", mock_fetch_yf
        )

        result = await tools["get_investment_opinions"]("AAPL", market="us")

        assert result["symbol"] == "AAPL"
        assert result["consensus"]["avg_target_price"] == 195.5
        assert result["consensus"]["current_price"] == 185.5
        assert result["consensus"]["upside_pct"] == 5.4

    async def test_us_market_skips_upside_when_avg_target_not_numeric(
        self, monkeypatch
    ):
        class MockTicker:
            def __init__(self, symbol: str, session=None):
                assert session is not None
                self.symbol = symbol
                self.analyst_price_targets = {
                    "mean": "N/A",
                    "median": 195.0,
                    "low": 180.0,
                    "high": 210.0,
                    "current": 185.5,
                }
                self.upgrades_downgrades = pd.DataFrame()
                self.info = {"currentPrice": 185.5}

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        assert result["consensus"]["avg_target_price"] is None
        assert result["consensus"]["current_price"] == 185.5
        assert result["consensus"]["upside_pct"] is None

    async def test_us_market_uses_recommendation_trend_counts_and_normalized_targets(
        self, monkeypatch
    ):
        class MockTicker:
            def __init__(self, symbol: str, session=None):
                assert session is not None
                self.symbol = symbol
                self.analyst_price_targets = {
                    "mean": {"raw": 200.0, "fmt": "200.00"},
                    "median": np.float64(198.0),
                    "low": {"raw": pd.Series([180.0], dtype="Float64").iloc[0]},
                    "high": np.float64(220.0),
                    "current": {"raw": 185.0, "fmt": "185.00"},
                }
                self.recommendations = pd.DataFrame(
                    [
                        {
                            "period": "-1m",
                            "strongBuy": 4,
                            "buy": 6,
                            "hold": 5,
                            "sell": 2,
                            "strongSell": 1,
                        },
                        {
                            "period": "0m",
                            "strongBuy": 5,
                            "buy": 7,
                            "hold": 8,
                            "sell": 3,
                            "strongSell": 2,
                        },
                    ]
                )
                self.upgrades_downgrades = pd.DataFrame()
                self.info = {"currentPrice": 184.0}

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        assert result["count"] == 0
        assert result["opinions"] == []
        assert result["consensus"]["buy_count"] == 12
        assert result["consensus"]["hold_count"] == 8
        assert result["consensus"]["sell_count"] == 5
        assert result["consensus"]["strong_buy_count"] == 5
        assert result["consensus"]["total_count"] == 25
        assert result["consensus"]["avg_target_price"] == 200.0
        assert result["consensus"]["median_target_price"] == 198.0
        assert result["consensus"]["min_target_price"] == 180.0
        assert result["consensus"]["max_target_price"] == 220.0
        assert result["consensus"]["current_price"] == 185.0
        assert result["consensus"]["upside_pct"] == 8.11

    async def test_us_market_returns_warning_for_unavailable_yahoo_consensus(
        self, monkeypatch
    ):
        class MockTicker:
            def __init__(self, symbol: str, session=None):
                assert session is not None
                self.symbol = symbol
                self.analyst_price_targets = {
                    "mean": {"raw": 0, "fmt": "0.00"},
                    "median": None,
                    "low": "",
                    "high": {"raw": 0},
                    "current": {"raw": 0, "fmt": "0.00"},
                }
                self.recommendations = pd.DataFrame(
                    [
                        {
                            "period": "0m",
                            "strongBuy": 0,
                            "buy": 0,
                            "hold": 0,
                            "sell": 0,
                            "strongSell": 0,
                        }
                    ]
                )
                self.upgrades_downgrades = pd.DataFrame()
                self.info = {"currentPrice": 185.0}

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        assert result["count"] == 0
        assert result["opinions"] == []
        assert result["consensus"]["buy_count"] is None
        assert result["consensus"]["hold_count"] is None
        assert result["consensus"]["sell_count"] is None
        assert result["consensus"]["strong_buy_count"] is None
        assert result["consensus"]["total_count"] is None
        assert result["consensus"]["avg_target_price"] is None
        assert result["consensus"]["median_target_price"] is None
        assert result["consensus"]["min_target_price"] is None
        assert result["consensus"]["max_target_price"] is None
        assert result["consensus"]["upside_pct"] is None
        assert "warning" in result
        assert "Yahoo" in result["warning"]

    async def test_us_market_normalizes_fmt_only_target_dicts(self, monkeypatch):
        class MockTicker:
            def __init__(self, symbol: str, session=None):
                assert session is not None
                self.symbol = symbol
                self.analyst_price_targets = {
                    "mean": {"fmt": "200.00"},
                    "median": {"fmt": "198.00"},
                    "low": {"fmt": "180.00"},
                    "high": {"fmt": "220.00"},
                    "current": {"fmt": "185.00"},
                }
                self.recommendations = pd.DataFrame(
                    [
                        {
                            "period": "0m",
                            "strongBuy": 5,
                            "buy": 7,
                            "hold": 8,
                            "sell": 3,
                            "strongSell": 2,
                        }
                    ]
                )
                self.upgrades_downgrades = pd.DataFrame()
                self.info = {"currentPrice": 184.0}

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        assert result["consensus"]["avg_target_price"] == 200.0
        assert result["consensus"]["median_target_price"] == 198.0
        assert result["consensus"]["min_target_price"] == 180.0
        assert result["consensus"]["max_target_price"] == 220.0
        assert result["consensus"]["current_price"] == 185.0
        assert result["consensus"]["upside_pct"] == 8.11

    async def test_us_market_keeps_partial_recommendation_counts_unavailable(
        self, monkeypatch
    ):
        class MockTicker:
            def __init__(self, symbol: str, session=None):
                assert session is not None
                self.symbol = symbol
                self.analyst_price_targets = {
                    "mean": 200.0,
                    "median": 198.0,
                    "low": 180.0,
                    "high": 220.0,
                    "current": 185.0,
                }
                self.recommendations = pd.DataFrame(
                    [
                        {
                            "period": "0m",
                            "strongBuy": 5,
                            "buy": 7,
                            "hold": np.nan,
                            "sell": 3,
                            "strongSell": 2,
                        }
                    ]
                )
                self.upgrades_downgrades = pd.DataFrame()
                self.info = {"currentPrice": 184.0}

        monkeypatch.setattr(yf, "Ticker", MockTicker)

        result = await fundamentals_sources_naver._fetch_investment_opinions_yfinance(
            "AAPL", 10
        )

        assert result["consensus"]["buy_count"] is None
        assert result["consensus"]["hold_count"] is None
        assert result["consensus"]["sell_count"] is None
        assert result["consensus"]["strong_buy_count"] is None
        assert result["consensus"]["total_count"] is None
        assert result["consensus"]["avg_target_price"] == 200.0
        assert result["consensus"]["current_price"] == 185.0


@pytest.mark.asyncio
class TestScreenEnrichmentHelpers:
    async def test_kr_screen_enrichment_uses_sector_and_normalized_consensus(
        self, monkeypatch
    ) -> None:
        async def mock_profile(symbol: str) -> dict[str, object]:
            assert symbol == "005930"
            return {"sector": "Semiconductors"}

        async def mock_opinions(symbol: str, limit: int) -> dict[str, object]:
            assert symbol == "005930"
            assert limit == 10
            return {
                "symbol": symbol,
                "count": 4,
                "consensus": {
                    "buy_count": 3,
                    "hold_count": 1,
                    "sell_count": 0,
                    "avg_target_price": 91000,
                    "upside_pct": 12.4,
                },
            }

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

        result = await fundamentals_sources_naver._fetch_screen_enrichment_kr("005930")

        assert result == {
            "sector": "Semiconductors",
            "analyst_buy": 3,
            "analyst_hold": 1,
            "analyst_sell": 0,
            "avg_target": 91000,
            "upside_pct": 12.4,
        }

    async def test_us_screen_enrichment_uses_yfinance_opinions_and_sector(
        self, monkeypatch
    ) -> None:
        async def mock_opinions(symbol: str, limit: int) -> dict[str, object]:
            assert symbol == "AAPL"
            assert limit == 10
            return {
                "symbol": symbol,
                "count": 5,
                "consensus": {
                    "buy_count": 2,
                    "hold_count": 2,
                    "sell_count": 1,
                    "avg_target_price": 245.0,
                    "upside_pct": 8.7,
                },
            }

        async def mock_profile(symbol: str) -> dict[str, object]:
            assert symbol == "AAPL"
            return {"sector": "Technology"}

        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_investment_opinions_yfinance",
            mock_opinions,
            raising=False,
        )
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_company_profile_finnhub",
            mock_profile,
            raising=False,
        )

        result = await fundamentals_sources_naver._fetch_screen_enrichment_us("AAPL")

        assert result == {
            "sector": "Technology",
            "analyst_buy": 2,
            "analyst_hold": 2,
            "analyst_sell": 1,
            "avg_target": 245.0,
            "upside_pct": 8.7,
        }

    async def test_screen_enrichment_defaults_when_opinions_missing(
        self, monkeypatch
    ) -> None:
        async def mock_opinions(symbol: str, limit: int) -> dict[str, object]:
            return {"symbol": symbol, "count": 0, "opinions": [], "consensus": None}

        async def mock_profile(symbol: str) -> dict[str, object]:
            return {"sector": None}

        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_investment_opinions_yfinance",
            mock_opinions,
            raising=False,
        )
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_company_profile_finnhub",
            mock_profile,
            raising=False,
        )

        result = await fundamentals_sources_naver._fetch_screen_enrichment_us("MSFT")

        assert result == {
            "sector": None,
            "analyst_buy": 0,
            "analyst_hold": 0,
            "analyst_sell": 0,
            "avg_target": None,
            "upside_pct": None,
        }

    async def test_kr_screen_enrichment_keeps_analyst_data_when_profile_fails(
        self, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def mock_profile(symbol: str) -> dict[str, object]:
            assert symbol == "005930"
            raise RuntimeError("profile unavailable")

        async def mock_opinions(symbol: str, limit: int) -> dict[str, object]:
            assert symbol == "005930"
            assert limit == 10
            return {
                "symbol": symbol,
                "count": 4,
                "consensus": {
                    "buy_count": 3,
                    "hold_count": 1,
                    "sell_count": 0,
                    "avg_target_price": 91000,
                    "upside_pct": 12.4,
                },
            }

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
        caplog.set_level("WARNING")

        result = await fundamentals_sources_naver._fetch_screen_enrichment_kr("005930")

        assert result == {
            "sector": None,
            "analyst_buy": 3,
            "analyst_hold": 1,
            "analyst_sell": 0,
            "avg_target": 91000,
            "upside_pct": 12.4,
        }
        assert any("profile unavailable" in message for message in caplog.messages)

    async def test_us_screen_enrichment_keeps_sector_when_opinions_fail(
        self, monkeypatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def mock_profile(symbol: str) -> dict[str, object]:
            assert symbol == "AAPL"
            return {"sector": "Technology"}

        async def mock_opinions(symbol: str, limit: int) -> dict[str, object]:
            assert symbol == "AAPL"
            assert limit == 10
            raise RuntimeError("opinions unavailable")

        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_company_profile_finnhub",
            mock_profile,
            raising=False,
        )
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_investment_opinions_yfinance",
            mock_opinions,
            raising=False,
        )
        caplog.set_level("WARNING")

        result = await fundamentals_sources_naver._fetch_screen_enrichment_us("AAPL")

        assert result == {
            "sector": "Technology",
            "analyst_buy": 0,
            "analyst_hold": 0,
            "analyst_sell": 0,
            "avg_target": None,
            "upside_pct": None,
        }
        assert any("opinions unavailable" in message for message in caplog.messages)

    async def test_screen_enrichment_raises_when_both_providers_fail(
        self, monkeypatch
    ) -> None:
        async def mock_profile(symbol: str) -> dict[str, object]:
            assert symbol == "MSFT"
            raise RuntimeError("profile unavailable")

        async def mock_opinions(symbol: str, limit: int) -> dict[str, object]:
            assert symbol == "MSFT"
            assert limit == 10
            raise RuntimeError("opinions unavailable")

        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_company_profile_finnhub",
            mock_profile,
            raising=False,
        )
        monkeypatch.setattr(
            fundamentals_sources_naver,
            "_fetch_investment_opinions_yfinance",
            mock_opinions,
            raising=False,
        )

        with pytest.raises(
            RuntimeError, match="profile unavailable|opinions unavailable"
        ):
            await fundamentals_sources_naver._fetch_screen_enrichment_us("MSFT")

    async def test_row_decoration_enriches_only_equities_and_collects_warnings(
        self, monkeypatch
    ) -> None:
        async def mock_kr(symbol: str) -> dict[str, object]:
            if symbol == "005930":
                return {
                    "sector": "Semiconductors",
                    "analyst_buy": 4,
                    "analyst_hold": 1,
                    "analyst_sell": 0,
                    "avg_target": 90000,
                    "upside_pct": 20.0,
                }
            raise RuntimeError("kr enrichment failed")

        async def mock_us(symbol: str) -> dict[str, object]:
            return {
                "sector": "Technology",
                "analyst_buy": 10,
                "analyst_hold": 3,
                "analyst_sell": 1,
                "avg_target": 250.0,
                "upside_pct": 5.0,
            }

        monkeypatch.setattr(
            analysis_screen_core,
            "_fetch_screen_enrichment_kr",
            mock_kr,
            raising=False,
        )
        monkeypatch.setattr(
            analysis_screen_core,
            "_fetch_screen_enrichment_us",
            mock_us,
            raising=False,
        )

        rows = [
            {"code": "005930", "market": "kr", "name": "Samsung"},
            {"code": "035420", "market": "kr", "name": "Naver"},
            {"code": "AAPL", "market": "us", "name": "Apple"},
            {"symbol": "KRW-BTC", "market": "crypto", "name": "Bitcoin"},
        ]

        (
            decorated,
            warnings,
        ) = await analysis_screen_core._decorate_screen_rows_with_equity_enrichment(
            rows,
            concurrency=2,
        )

        assert decorated[0]["sector"] == "Semiconductors"
        assert decorated[0]["analyst_buy"] == 4
        assert decorated[1]["sector"] is None
        assert decorated[1]["analyst_buy"] is None
        assert decorated[2]["sector"] == "Technology"
        assert decorated[2]["avg_target"] == 250.0
        assert decorated[3]["sector"] is None
        assert decorated[3]["analyst_buy"] is None
        assert warnings == ["kr:035420: RuntimeError: kr enrichment failed"]

    async def test_analyze_stock_generates_recommendation_kr(self):
        """Test that _build_recommendation_for_equity generates recommendation for Korean stocks."""
        mock_analysis = {
            "symbol": "005930",
            "market_type": "equity_kr",
            "source": "kis",
            "quote": {"price": 75000},
            "indicators": {
                "indicators": {
                    "rsi": {"14": 45.0},
                    "bollinger": {"lower": 74000, "middle": 75000, "upper": 76000},
                }
            },
            "support_resistance": {
                "supports": [{"price": 73000}],
                "resistances": [{"price": 77000}],
            },
            "opinions": {
                "consensus": {
                    "buy_count": 2,
                    "sell_count": 0,
                    "total_count": 2,
                    "avg_target_price": 85000,
                    "current_price": 75000,
                }
            },
        }

        # Test _build_recommendation_for_equity directly
        recommendation = shared.build_recommendation_for_equity(
            mock_analysis, "equity_kr"
        )

        assert recommendation is not None
        assert "action" in recommendation
        assert recommendation["action"] in ("buy", "hold", "sell")
        assert "confidence" in recommendation
        assert "buy_zones" in recommendation
        assert "sell_targets" in recommendation
        assert "stop_loss" in recommendation
        assert "reasoning" in recommendation

    async def test_analyze_stock_no_recommendation_crypto(self, monkeypatch):
        """Test that analyze_stock does not generate recommendation for crypto."""
        tools = build_tools()

        mock_analysis = {
            "symbol": "KRW-BTC",
            "market_type": "crypto",
            "source": "upbit",
            "quote": {"price": 80000000},
        }

        async def mock_impl(s, m, i):
            return mock_analysis

        _patch_runtime_attr(monkeypatch, "_analyze_stock_impl", mock_impl)

        result = await tools["analyze_stock"]("KRW-BTC")

        assert "recommendation" not in result


@pytest.mark.asyncio
async def test_analyze_stock_us_reuses_preloaded_yfinance_analyst_snapshot(
    monkeypatch,
):
    tools = build_tools()
    yf_calls = {"info": 0, "targets": 0, "recommendations": 0, "ud": 0}

    async def mock_fetch_ohlcv(symbol, market_type, count):
        return pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=250, freq="D"),
                "open": [100.0] * 250,
                "high": [105.0] * 250,
                "low": [95.0] * 250,
                "close": [102.0] * 250,
                "volume": [1000] * 250,
            }
        )

    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv)

    async def mock_get_quote(symbol, market_type):
        return {
            "symbol": symbol,
            "instrument_type": "equity_us",
            "price": 150.0,
            "source": "yahoo",
        }

    monkeypatch.setattr(analysis_analyze, "_get_quote_impl", mock_get_quote)

    async def mock_get_indicators(symbol, indicators, market=None, preloaded_df=None):
        assert preloaded_df is not None
        return {"indicators": {"rsi": {"14": 45.0}}}

    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", mock_get_indicators)

    async def mock_get_support_resistance(symbol, market=None, preloaded_df=None):
        assert preloaded_df is not None
        return {"supports": [], "resistances": []}

    monkeypatch.setattr(
        analysis_analyze,
        "_get_support_resistance_impl",
        mock_get_support_resistance,
    )
    monkeypatch.setattr(
        analysis_analyze,
        "_build_recommendation_for_equity",
        lambda analysis, market_type: None,
    )

    class MockKISClient:
        pass

    _patch_runtime_attr(monkeypatch, "KISClient", MockKISClient)

    def mock_yf_ticker(symbol, session=None):
        class MockTicker:
            @property
            def info(self):
                yf_calls["info"] += 1
                return {
                    "shortName": f"{symbol} Inc",
                    "currentPrice": 150.0,
                    "fiftyTwoWeekHigh": 200.0,
                    "fiftyTwoWeekLow": 100.0,
                    "trailingPE": 25.0,
                    "priceToBook": 5.0,
                    "returnOnEquity": 0.15,
                    "dividendYield": 0.01,
                }

            @property
            def analyst_price_targets(self):
                yf_calls["targets"] += 1
                return {
                    "mean": {"raw": 180.0, "fmt": "180.00"},
                    "median": 175.0,
                    "low": 150.0,
                    "high": 200.0,
                    "current": 150.0,
                }

            @property
            def recommendations(self):
                yf_calls["recommendations"] += 1
                return pd.DataFrame(
                    [
                        {
                            "period": "0m",
                            "strongBuy": 3,
                            "buy": 4,
                            "hold": 2,
                            "sell": 1,
                            "strongSell": 0,
                        }
                    ]
                )

            @property
            def upgrades_downgrades(self):
                yf_calls["ud"] += 1
                return pd.DataFrame(
                    [
                        {
                            "Firm": "Firm A",
                            "ToGrade": "Hold",
                            "GradeDate": pd.Timestamp("2024-01-02"),
                            "currentPriceTarget": 160.0,
                        }
                    ]
                )

        return MockTicker()

    monkeypatch.setattr(fundamentals_sources_naver.yf, "Ticker", mock_yf_ticker)

    class MockFinnhubClient:
        def company_profile2(self, symbol):
            return {"name": f"{symbol} Inc", "ticker": symbol}

        def general_news(self, category, min_id=0):
            return []

        def company_news(self, symbol, _from, to):
            return []

    monkeypatch.setattr(
        fundamentals_sources_naver,
        "_get_finnhub_client",
        MockFinnhubClient,
    )

    result = await tools["analyze_stock"]("AAPL", market="us")

    assert yf_calls == {"info": 1, "targets": 1, "recommendations": 1, "ud": 1}

    assert result["symbol"] == "AAPL"
    assert "valuation" in result
    assert "opinions" in result
    assert result["opinions"]["count"] == 1
    assert result["opinions"]["opinions"][0]["firm"] == "Firm A"
    assert result["opinions"]["consensus"]["buy_count"] == 7
    assert result["opinions"]["consensus"]["hold_count"] == 2
    assert result["opinions"]["consensus"]["sell_count"] == 1
    assert result["opinions"]["consensus"]["strong_buy_count"] == 3
    assert result["opinions"]["consensus"]["total_count"] == 10


@pytest.mark.asyncio
async def test_analyze_stock_kr_reuses_preloaded_ohlcv_and_bundled_naver(monkeypatch):
    tools = build_tools()

    ohlcv_fetches: list[tuple[str, str, int]] = []

    async def mock_fetch_ohlcv(symbol, market_type, count):
        ohlcv_fetches.append((symbol, market_type, count))
        return pd.DataFrame(
            {
                "date": pd.to_datetime(["2024-01-01"]),
                "open": [74000.0],
                "high": [76000.0],
                "low": [73000.0],
                "close": [75000.0],
                "volume": [1000000],
                "value": [75000000000.0],
            }
        )

    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv)

    quote_mock = AsyncMock(side_effect=AssertionError("unexpected KR quote fetch"))
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", quote_mock)

    standalone_valuation_mock = AsyncMock(
        side_effect=AssertionError("unexpected standalone KR valuation fetch")
    )
    _patch_runtime_attr(
        monkeypatch, "_fetch_valuation_naver", standalone_valuation_mock
    )

    standalone_news_mock = AsyncMock(
        side_effect=AssertionError("unexpected standalone KR news fetch")
    )
    _patch_runtime_attr(monkeypatch, "_fetch_news_naver", standalone_news_mock)

    standalone_opinions_mock = AsyncMock(
        side_effect=AssertionError("unexpected standalone KR opinions fetch")
    )
    _patch_runtime_attr(
        monkeypatch, "_fetch_investment_opinions_naver", standalone_opinions_mock
    )

    bundle_mock = AsyncMock(
        return_value={
            "valuation": {
                "instrument_type": "equity_kr",
                "source": "naver",
                "symbol": "005930",
                "current_price": 75000,
                "per": 12.5,
            },
            "news": {
                "symbol": "005930",
                "market": "kr",
                "source": "naver",
                "count": 1,
                "news": [{"title": "headline", "url": "https://example.com/news"}],
            },
            "opinions": {
                "instrument_type": "equity_kr",
                "source": "naver",
                "symbol": "005930",
                "count": 2,
                "opinions": [
                    {"firm": "Firm A", "rating": "Buy", "target_price": 85000},
                    {"firm": "Firm B", "rating": "Strong Buy", "target_price": 90000},
                ],
                "consensus": {
                    "buy_count": 2,
                    "hold_count": 0,
                    "sell_count": 0,
                    "total_count": 2,
                    "avg_target_price": 87500,
                    "current_price": 75000,
                },
            },
        }
    )
    monkeypatch.setattr(
        analysis_analyze,
        "_fetch_analysis_snapshot_naver",
        bundle_mock,
        raising=False,
    )

    async def mock_get_indicators(symbol, indicators, market=None, preloaded_df=None):
        assert symbol == "005930"
        assert preloaded_df is not None
        return {"indicators": {"rsi": {"14": 45.0}}}

    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", mock_get_indicators)

    async def mock_get_support_resistance(symbol, market=None, preloaded_df=None):
        assert symbol == "005930"
        assert preloaded_df is not None
        return {"supports": [{"price": 73000}], "resistances": [{"price": 77000}]}

    monkeypatch.setattr(
        analysis_analyze,
        "_get_support_resistance_impl",
        mock_get_support_resistance,
    )
    monkeypatch.setattr(
        analysis_analyze,
        "_build_recommendation_for_equity",
        lambda analysis, market_type: None,
    )

    result = await tools["analyze_stock"]("005930", market="kr")

    assert ohlcv_fetches == [("005930", "equity_kr", 250)]
    quote_mock.assert_not_awaited()
    standalone_valuation_mock.assert_not_awaited()
    standalone_news_mock.assert_not_awaited()
    standalone_opinions_mock.assert_not_awaited()
    bundle_mock.assert_awaited_once_with("005930", 5, 10)
    assert result["symbol"] == "005930"
    assert result["market_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["quote"] == {
        "symbol": "005930",
        "instrument_type": "equity_kr",
        "price": 75000.0,
        "open": 74000.0,
        "high": 76000.0,
        "low": 73000.0,
        "volume": 1000000,
        "value": 75000000000.0,
        "source": "kis",
    }
    assert result["valuation"]["instrument_type"] == "equity_kr"
    assert result["news"]["source"] == "naver"
    assert result["opinions"]["source"] == "naver"
    assert result["errors"] == []


@pytest.mark.asyncio
async def test_analyze_stock_kr_falls_back_to_quote_helper_when_ohlcv_empty(
    monkeypatch,
):
    tools = build_tools()

    async def mock_fetch_ohlcv(symbol, market_type, count):
        _ = symbol, market_type, count
        return pd.DataFrame(
            columns=["date", "open", "high", "low", "close", "volume", "value"]
        )

    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv)

    quote_mock = AsyncMock(
        return_value={
            "symbol": "005930",
            "instrument_type": "equity_kr",
            "price": 75100.0,
            "open": 74100.0,
            "high": 76100.0,
            "low": 73100.0,
            "volume": 1100000,
            "value": 76000000000.0,
            "source": "kis",
        }
    )
    _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", quote_mock)

    standalone_valuation_mock = AsyncMock(
        side_effect=AssertionError("unexpected standalone KR valuation fetch")
    )
    _patch_runtime_attr(
        monkeypatch, "_fetch_valuation_naver", standalone_valuation_mock
    )
    standalone_news_mock = AsyncMock(
        side_effect=AssertionError("unexpected standalone KR news fetch")
    )
    _patch_runtime_attr(monkeypatch, "_fetch_news_naver", standalone_news_mock)
    standalone_opinions_mock = AsyncMock(
        side_effect=AssertionError("unexpected standalone KR opinions fetch")
    )
    _patch_runtime_attr(
        monkeypatch, "_fetch_investment_opinions_naver", standalone_opinions_mock
    )

    bundle_mock = AsyncMock(
        return_value={
            "valuation": {
                "instrument_type": "equity_kr",
                "source": "naver",
                "symbol": "005930",
                "current_price": 75100,
            },
            "news": {
                "symbol": "005930",
                "market": "kr",
                "source": "naver",
                "count": 0,
                "news": [],
            },
            "opinions": {
                "instrument_type": "equity_kr",
                "source": "naver",
                "symbol": "005930",
                "count": 0,
                "opinions": [],
                "consensus": {"total_count": 0, "current_price": 75100},
            },
        }
    )
    monkeypatch.setattr(
        analysis_analyze,
        "_fetch_analysis_snapshot_naver",
        bundle_mock,
        raising=False,
    )

    async def mock_get_indicators(symbol, indicators, market=None, preloaded_df=None):
        assert preloaded_df is not None
        return {"indicators": {"rsi": {"14": None}}}

    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", mock_get_indicators)

    async def mock_get_support_resistance(symbol, market=None, preloaded_df=None):
        assert preloaded_df is not None
        return {"supports": [], "resistances": []}

    monkeypatch.setattr(
        analysis_analyze,
        "_get_support_resistance_impl",
        mock_get_support_resistance,
    )
    monkeypatch.setattr(
        analysis_analyze,
        "_build_recommendation_for_equity",
        lambda analysis, market_type: None,
    )

    result = await tools["analyze_stock"]("005930", market="kr")

    quote_mock.assert_awaited_once_with("005930")
    standalone_valuation_mock.assert_not_awaited()
    standalone_news_mock.assert_not_awaited()
    standalone_opinions_mock.assert_not_awaited()
    bundle_mock.assert_awaited_once_with("005930", 5, 10)
    assert result["quote"] == quote_mock.return_value
    assert result["valuation"]["source"] == "naver"
    assert result["news"]["source"] == "naver"
    assert result["opinions"]["source"] == "naver"


@pytest.mark.asyncio
async def test_analyze_stock_crypto_uses_extended_default_indicators(monkeypatch):
    tools = build_tools()
    indicator_calls: list[dict[str, object]] = []
    expected_indicators = ["rsi", "macd", "bollinger", "sma", "adx", "stoch_rsi"]

    async def mock_fetch_ohlcv(symbol, market_type, count):
        assert symbol == "KRW-BTC"
        assert market_type == "crypto"
        assert count == 250
        return pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=250, freq="D"),
                "open": [100.0] * 250,
                "high": [110.0] * 250,
                "low": [90.0] * 250,
                "close": [105.0] * 250,
                "volume": [1000.0] * 250,
                "value": [105000.0] * 250,
            }
        )

    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv)
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_quote_crypto",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "instrument_type": "crypto",
                "price": 105.0,
                "source": "upbit",
            }
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_news_finnhub",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "market": "crypto",
                "source": "finnhub",
                "count": 0,
                "news": [],
            }
        ),
    )

    async def mock_get_indicators(symbol, indicators, market=None, preloaded_df=None):
        indicator_calls.append(
            {
                "symbol": symbol,
                "indicators": list(indicators),
                "market": market,
                "has_preloaded_df": preloaded_df is not None,
            }
        )
        return {
            "indicators": {
                "rsi": {"14": 48.2},
                "macd": {"macd": 1.0, "signal": 0.8, "histogram": 0.2},
                "bollinger": {"upper": 112.0, "middle": 105.0, "lower": 98.0},
                "sma": {"5": 104.0, "20": 101.0},
                "adx": {"adx": 27.4, "plus_di": 31.2, "minus_di": 18.7},
                "stoch_rsi": {"k": 61.5, "d": 55.1},
            }
        }

    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", mock_get_indicators)

    async def mock_get_support_resistance(symbol, market=None, preloaded_df=None):
        assert symbol == "KRW-BTC"
        assert preloaded_df is not None
        return {"supports": [{"price": 95.0}], "resistances": [{"price": 115.0}]}

    monkeypatch.setattr(
        analysis_analyze,
        "_get_support_resistance_impl",
        mock_get_support_resistance,
    )

    result = await tools["analyze_stock"]("KRW-BTC", market="crypto")

    assert indicator_calls == [
        {
            "symbol": "KRW-BTC",
            "indicators": expected_indicators,
            "market": None,
            "has_preloaded_df": True,
        }
    ]
    assert result["indicators"]["indicators"]["adx"] == {
        "adx": 27.4,
        "plus_di": 31.2,
        "minus_di": 18.7,
    }
    assert result["indicators"]["indicators"]["stoch_rsi"] == {"k": 61.5, "d": 55.1}
    assert result["errors"] == []
    assert "recommendation" not in result


@pytest.mark.asyncio
async def test_analyze_portfolio_crypto_reuses_analyze_stock_default_indicators(
    monkeypatch,
):
    tools = build_tools()
    indicator_calls: list[list[str]] = []
    fanout_calls: list[tuple[str, str | None, bool]] = []
    expected_indicators = ["rsi", "macd", "bollinger", "sma", "adx", "stoch_rsi"]

    async def mock_fetch_ohlcv(symbol, market_type, count):
        assert symbol == "KRW-BTC"
        assert market_type == "crypto"
        assert count == 250
        return pd.DataFrame(
            {
                "date": pd.date_range("2024-01-01", periods=250, freq="D"),
                "open": [100.0] * 250,
                "high": [110.0] * 250,
                "low": [90.0] * 250,
                "close": [105.0] * 250,
                "volume": [1000.0] * 250,
                "value": [105000.0] * 250,
            }
        )

    _patch_runtime_attr(monkeypatch, "_fetch_ohlcv_for_indicators", mock_fetch_ohlcv)
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_quote_crypto",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "instrument_type": "crypto",
                "price": 105.0,
                "source": "upbit",
            }
        ),
    )
    _patch_runtime_attr(
        monkeypatch,
        "_fetch_news_finnhub",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "market": "crypto",
                "source": "finnhub",
                "count": 0,
                "news": [],
            }
        ),
    )

    async def mock_get_indicators(symbol, indicators, market=None, preloaded_df=None):
        assert symbol == "KRW-BTC"
        assert preloaded_df is not None
        indicator_calls.append(list(indicators))
        return {
            "indicators": {
                "adx": {"adx": 27.4, "plus_di": 31.2, "minus_di": 18.7},
                "stoch_rsi": {"k": 61.5, "d": 55.1},
            }
        }

    monkeypatch.setattr(analysis_analyze, "_get_indicators_impl", mock_get_indicators)
    monkeypatch.setattr(
        analysis_analyze,
        "_get_support_resistance_impl",
        AsyncMock(return_value={"supports": [], "resistances": []}),
    )

    real_analyze_stock_impl = analysis_screening._analyze_stock_impl

    async def tracking_analyze_stock_impl(
        symbol: str, market: str | None, include_peers: bool
    ):
        fanout_calls.append((symbol, market, include_peers))
        return await real_analyze_stock_impl(symbol, market, include_peers)

    monkeypatch.setattr(
        analysis_screening,
        "_analyze_stock_impl",
        tracking_analyze_stock_impl,
    )

    result = await tools["analyze_portfolio"](["KRW-BTC"], market="crypto")

    assert fanout_calls == [("KRW-BTC", "crypto", False)]
    assert indicator_calls == [expected_indicators]
    assert result["summary"] == {
        "total_symbols": 1,
        "successful": 1,
        "failed": 0,
        "errors": [],
    }
    assert result["results"]["KRW-BTC"]["indicators"]["indicators"]["adx"] == {
        "adx": 27.4,
        "plus_di": 31.2,
        "minus_di": 18.7,
    }
    assert result["results"]["KRW-BTC"]["indicators"]["indicators"]["stoch_rsi"] == {
        "k": 61.5,
        "d": 55.1,
    }


# ---------------------------------------------------------------------------
# TestParseNaverNum
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseNaverNum:
    """Tests for _parse_naver_num and _parse_naver_int."""

    def test_none(self):
        assert fundamentals_sources_naver._parse_naver_num(None) is None
        assert fundamentals_sources_naver._parse_naver_int(None) is None

    def test_numeric(self):
        assert fundamentals_sources_naver._parse_naver_num(1234.5) == 1234.5
        assert fundamentals_sources_naver._parse_naver_num(100) == 100.0
        assert fundamentals_sources_naver._parse_naver_int(42) == 42

    def test_string_with_commas(self):
        assert fundamentals_sources_naver._parse_naver_num("2,450.50") == 2450.50
        assert fundamentals_sources_naver._parse_naver_num("-45.30") == -45.30
        assert fundamentals_sources_naver._parse_naver_int("450,000,000") == 450000000

    def test_invalid_string(self):
        assert fundamentals_sources_naver._parse_naver_num("abc") is None
        assert fundamentals_sources_naver._parse_naver_int("abc") is None


# ---------------------------------------------------------------------------
# TestIndexMeta
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestIndexMeta:
    """Tests for _INDEX_META and _DEFAULT_INDICES."""

    def test_all_default_indices_have_meta(self):
        for sym in fundamentals_sources_indices._DEFAULT_INDICES:
            assert sym in fundamentals_sources_indices._INDEX_META

    def test_korean_indices_have_naver_code(self):
        for sym in ("KOSPI", "KOSDAQ"):
            meta = fundamentals_sources_indices._INDEX_META[sym]
            assert meta["source"] == "naver"
            assert "naver_code" in meta

    def test_us_indices_have_yf_ticker(self):
        for sym in ("SPX", "NASDAQ", "DJI"):
            meta = fundamentals_sources_indices._INDEX_META[sym]
            assert meta["source"] == "yfinance"
            assert "yf_ticker" in meta

    def test_aliases(self):
        assert (
            fundamentals_sources_indices._INDEX_META["SPX"]["yf_ticker"]
            == fundamentals_sources_indices._INDEX_META["SP500"]["yf_ticker"]
        )
        assert (
            fundamentals_sources_indices._INDEX_META["DJI"]["yf_ticker"]
            == fundamentals_sources_indices._INDEX_META["DOW"]["yf_ticker"]
        )


# ---------------------------------------------------------------------------
# TestCalculateFibonacci
# ---------------------------------------------------------------------------


def _fib_df_uptrend(n: int = 60) -> pd.DataFrame:
    """Create OHLCV DataFrame where low comes first, then high (uptrend)."""
    import datetime as dt

    import numpy as np

    dates = [dt.date.today() - dt.timedelta(days=n - 1 - i) for i in range(n)]
    # Price goes from 100 up to ~200
    close = np.linspace(100, 200, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close - 1,
            "high": close + 2,
            "low": close - 3,
            "close": close,
            "volume": [1000] * n,
        }
    )


def _fib_df_downtrend(n: int = 60) -> pd.DataFrame:
    """Create OHLCV DataFrame where high comes first, then low (downtrend)."""
    import datetime as dt

    import numpy as np

    dates = [dt.date.today() - dt.timedelta(days=n - 1 - i) for i in range(n)]
    # Price goes from 200 down to ~100
    close = np.linspace(200, 100, n)
    return pd.DataFrame(
        {
            "date": dates,
            "open": close + 1,
            "high": close + 3,
            "low": close - 2,
            "close": close,
            "volume": [1000] * n,
        }
    )


@pytest.mark.unit
class TestCalculateFibonacci:
    """Tests for _calculate_fibonacci helper."""

    def test_uptrend_retracement_from_high(self):
        df = _fib_df_uptrend()
        current_price = float(df["close"].iloc[-1])
        result = market_data_indicators._calculate_fibonacci(df, current_price)

        assert result["trend"] == "retracement_from_high"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing high, 100% level = swing low
        assert result["levels"]["0.0"] > result["levels"]["1.0"]

    def test_downtrend_bounce_from_low(self):
        df = _fib_df_downtrend()
        current_price = float(df["close"].iloc[-1])
        result = market_data_indicators._calculate_fibonacci(df, current_price)

        assert result["trend"] == "bounce_from_low"
        assert result["swing_high"]["price"] > result["swing_low"]["price"]
        # 0% level = swing low, 100% level = swing high
        assert result["levels"]["0.0"] < result["levels"]["1.0"]

    def test_all_seven_levels_present(self):
        df = _fib_df_uptrend()
        result = market_data_indicators._calculate_fibonacci(df, 150.0)

        expected_keys = {"0.0", "0.236", "0.382", "0.5", "0.618", "0.786", "1.0"}
        assert set(result["levels"].keys()) == expected_keys

    def test_nearest_support_and_resistance(self):
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        swing_low = float(df["low"].min())
        mid = (swing_high + swing_low) / 2
        result = market_data_indicators._calculate_fibonacci(df, mid)

        if result["nearest_support"] is not None:
            assert result["nearest_support"]["price"] < mid
        if result["nearest_resistance"] is not None:
            assert result["nearest_resistance"]["price"] > mid

    def test_dates_are_strings(self):
        df = _fib_df_uptrend()
        result = market_data_indicators._calculate_fibonacci(df, 150.0)

        assert isinstance(result["swing_high"]["date"], str)
        assert isinstance(result["swing_low"]["date"], str)
        # ISO date format check
        assert len(result["swing_high"]["date"]) == 10
        assert len(result["swing_low"]["date"]) == 10

    def test_price_at_exact_level_no_crash(self):
        """If current price matches a level exactly, no crash."""
        df = _fib_df_uptrend()
        swing_high = float(df["high"].max())
        result = market_data_indicators._calculate_fibonacci(df, swing_high)

        assert result["current_price"] == swing_high


# ---------------------------------------------------------------------------
# TestComputeRsiWeights
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestComputeRsiWeights:
    """Tests for _compute_rsi_weights helper function."""

    def test_oversold_returns_front_heavy_weights(self):
        """RSI < 30: linear decreasing weights (more early)."""
        result = market_data_indicators._compute_rsi_weights(25.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # Front-heavy: first step gets most weight
        assert result[0] > result[1] > result[2]

    def test_oversold_with_four_splits(self):
        """RSI < 30 with splits=4."""
        result = market_data_indicators._compute_rsi_weights(28.0, 4)

        assert len(result) == 4
        assert abs(sum(result) - 1.0) < 0.001
        # Front-heavy: first > last, monotonically decreasing
        assert result[0] > result[-1]
        assert result[0] > result[1] > result[2] > result[3]

    def test_overbought_returns_back_heavy_weights(self):
        """RSI > 50: linear increasing weights (more later)."""
        result = market_data_indicators._compute_rsi_weights(65.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # Back-heavy: last step gets most weight
        assert result[2] > result[1] > result[0]

    def test_neutral_returns_equal_weights(self):
        """RSI 30-50: equal distribution."""
        result = market_data_indicators._compute_rsi_weights(40.0, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # All weights equal
        assert all(abs(w - result[0]) < 0.001 for w in result)

    def test_none_rsi_returns_equal_weights(self):
        """None RSI: equal distribution (same as neutral)."""
        result = market_data_indicators._compute_rsi_weights(None, 3)

        assert len(result) == 3
        assert abs(sum(result) - 1.0) < 0.001
        # All weights equal
        expected_weight = 1.0 / 3
        assert all(abs(w - expected_weight) < 0.001 for w in result)
