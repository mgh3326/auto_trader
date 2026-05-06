"""Tests for MCP server shared utilities, normalizers, and helper functions.

This module tests the utility functions in app.mcp_server.tooling.shared including:
- Market normalization (normalize_market, resolve_market_type)
- Symbol detection (is_korean_equity_code, is_crypto_market, is_us_equity_symbol)
- Value normalization (normalize_value, normalize_rows)
- Error payload creation (error_payload)
- Symbol normalization integration with tools
"""

from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from app.mcp_server.tooling import shared
from app.services import naver_finance
from tests._mcp_tooling_support import _patch_runtime_attr, build_tools

# ---------------------------------------------------------------------------
# Market Normalization Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizeMarket:
    """Tests for normalize_market helper function."""

    def test_returns_none_for_empty(self):
        assert shared.normalize_market(None) is None
        assert shared.normalize_market("") is None
        assert shared.normalize_market("   ") is None

    def test_crypto_aliases(self):
        for alias in ["crypto", "upbit", "krw", "usdt"]:
            assert shared.normalize_market(alias) == "crypto"

    def test_equity_kr_aliases(self):
        for alias in ["kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr"]:
            assert shared.normalize_market(alias) == "equity_kr"

    def test_equity_us_aliases(self):
        for alias in ["us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"]:
            assert shared.normalize_market(alias) == "equity_us"

    def test_case_insensitive(self):
        assert shared.normalize_market("CRYPTO") == "crypto"
        assert shared.normalize_market("KR") == "equity_kr"
        assert shared.normalize_market("Us") == "equity_us"

    def test_unknown_returns_none(self):
        assert shared.normalize_market("unknown") is None
        assert shared.normalize_market("invalid") is None


# ---------------------------------------------------------------------------
# Symbol Detection Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSymbolDetection:
    """Tests for symbol detection helper functions."""

    def test_is_korean_equity_code(self):
        # Regular stocks (6 digits)
        assert shared.is_korean_equity_code("005930") is True
        assert shared.is_korean_equity_code("000660") is True
        assert shared.is_korean_equity_code("  005930  ") is True
        # ETF/ETN (6 alphanumeric)
        assert shared.is_korean_equity_code("0123G0") is True  # ETF
        assert shared.is_korean_equity_code("0117V0") is True  # ETF
        assert shared.is_korean_equity_code("12345A") is True  # alphanumeric
        assert shared.is_korean_equity_code("0123g0") is True  # lowercase
        # A-prefixed Korean symbols (7 chars: 'A' + 6 digits)
        assert shared.is_korean_equity_code("A196170") is True
        assert shared.is_korean_equity_code("A005930") is True
        assert shared.is_korean_equity_code("A000660") is True
        assert shared.is_korean_equity_code("  A196170  ") is True  # whitespace
        # A-prefix with non-digit suffix -> NOT Korean
        assert shared.is_korean_equity_code("AAPLXYZ") is False  # not A + 6 digits
        assert shared.is_korean_equity_code("A12345") is False  # only 5 digits after A
        assert shared.is_korean_equity_code("A1234567") is False  # 7 digits after A
        # Invalid codes
        assert shared.is_korean_equity_code("00593") is False  # 5 chars
        assert shared.is_korean_equity_code("0059300") is False  # 7 chars
        assert shared.is_korean_equity_code("AAPL") is False  # 4 chars
        assert shared.is_korean_equity_code("0123-0") is False  # contains hyphen

    def test_is_crypto_market(self):
        assert shared.is_crypto_market("KRW-BTC") is True
        assert shared.is_crypto_market("krw-btc") is True
        assert shared.is_crypto_market("USDT-BTC") is True
        assert shared.is_crypto_market("usdt-eth") is True
        assert shared.is_crypto_market("BTC") is False
        assert shared.is_crypto_market("AAPL") is False
        assert shared.is_crypto_market("005930") is False

    def test_is_us_equity_symbol(self):
        assert shared.is_us_equity_symbol("AAPL") is True
        assert shared.is_us_equity_symbol("MSFT") is True
        assert shared.is_us_equity_symbol("BRK.B") is True
        assert shared.is_us_equity_symbol("KRW-BTC") is False  # crypto prefix
        assert shared.is_us_equity_symbol("005930") is False  # all digits


# ---------------------------------------------------------------------------
# Value Normalization Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizeValue:
    """Tests for normalize_value helper function."""

    def test_none_returns_none(self):
        assert shared.normalize_value(None) is None

    def test_nan_returns_none(self):
        import numpy as np

        assert shared.normalize_value(float("nan")) is None
        assert shared.normalize_value(np.nan) is None

    def test_datetime_returns_isoformat(self):
        import datetime

        dt = datetime.datetime(2024, 1, 15, 10, 30, 0)
        assert shared.normalize_value(dt) == "2024-01-15T10:30:00"

        d = datetime.date(2024, 1, 15)
        assert shared.normalize_value(d) == "2024-01-15"

    def test_timedelta_returns_seconds(self):
        td = pd.Timedelta(hours=1, minutes=30)
        assert shared.normalize_value(td) == pytest.approx(5400.0)

    def test_numpy_scalar_returns_python_type(self):
        import numpy as np

        assert shared.normalize_value(np.int64(42)) == 42
        assert shared.normalize_value(np.float64(3.14)) == pytest.approx(3.14)

    def test_regular_values_pass_through(self):
        assert shared.normalize_value(42) == 42
        assert shared.normalize_value(3.14) == pytest.approx(3.14)
        assert shared.normalize_value("hello") == "hello"


# ---------------------------------------------------------------------------
# Market Type Resolution Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestResolveMarketType:
    """Tests for resolve_market_type helper function."""

    def test_explicit_crypto_normalizes_symbol(self):
        market_type, symbol = shared.resolve_market_type("krw-btc", "crypto")
        assert market_type == "crypto"
        assert symbol == "KRW-BTC"

    def test_explicit_crypto_rejects_invalid_prefix(self):
        with pytest.raises(ValueError, match="KRW-/USDT- prefix"):
            shared.resolve_market_type("BTC", "crypto")

    def test_explicit_equity_kr_validates_digits(self):
        market_type, symbol = shared.resolve_market_type("005930", "kr")
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_explicit_equity_kr_validates_etf(self):
        """Test explicit market=kr with ETF alphanumeric code."""
        market_type, symbol = shared.resolve_market_type("0123G0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_explicit_equity_kr_validates_etf_lowercase(self):
        """Test explicit market=kr with lowercase ETF code (should be accepted)."""
        market_type, symbol = shared.resolve_market_type("0123g0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123g0"

    def test_explicit_equity_kr_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="6 alphanumeric"):
            shared.resolve_market_type("AAPL", "kr")

    def test_explicit_equity_us_rejects_crypto_prefix(self):
        with pytest.raises(ValueError, match="must not include KRW-/USDT-"):
            shared.resolve_market_type("KRW-BTC", "us")

    def test_auto_detect_crypto(self):
        market_type, symbol = shared.resolve_market_type("krw-eth", None)
        assert market_type == "crypto"
        assert symbol == "KRW-ETH"

    def test_auto_detect_korean_equity(self):
        market_type, symbol = shared.resolve_market_type("005930", None)
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_auto_detect_korean_etf(self):
        """Test auto-detection of Korean ETF code (alphanumeric)."""
        market_type, symbol = shared.resolve_market_type("0123G0", None)
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_auto_detect_korean_etf_another(self):
        """Test auto-detection with another ETF code pattern."""
        market_type, symbol = shared.resolve_market_type("0117V0", None)
        assert market_type == "equity_kr"
        assert symbol == "0117V0"

    def test_auto_detect_us_equity(self):
        market_type, symbol = shared.resolve_market_type("AAPL", None)
        assert market_type == "equity_us"
        assert symbol == "AAPL"

    def test_unsupported_symbol_raises(self):
        with pytest.raises(ValueError, match="Unsupported symbol format"):
            shared.resolve_market_type("1234", None)

    def test_market_aliases(self):
        # Test various market aliases
        assert shared.resolve_market_type("KRW-BTC", "upbit")[0] == "crypto"
        assert shared.resolve_market_type("005930", "kospi")[0] == "equity_kr"
        assert shared.resolve_market_type("AAPL", "nasdaq")[0] == "equity_us"

    def test_resolve_a_prefixed_kr_symbol_auto(self):
        """A-prefixed KR symbols auto-detected and normalized."""
        market_type, symbol = shared.resolve_market_type("A196170", None)
        assert market_type == "equity_kr"
        assert symbol == "196170"  # A prefix stripped

    def test_resolve_a_prefixed_kr_symbol_explicit_market(self):
        """A-prefixed KR symbols normalized when market is explicit."""
        market_type, symbol = shared.resolve_market_type("A005930", "kr")
        assert market_type == "equity_kr"
        assert symbol == "005930"  # A prefix stripped

    def test_resolve_a_prefixed_kr_symbol_whitespace(self):
        """Whitespace around A-prefixed symbol handled."""
        market_type, symbol = shared.resolve_market_type("  A196170  ", None)
        assert market_type == "equity_kr"
        assert symbol == "196170"


# ---------------------------------------------------------------------------
# Error Payload Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestErrorPayload:
    """Tests for error_payload helper function."""

    def test_minimal_payload(self):
        result = shared.error_payload(source="test", message="error occurred")
        assert result == {"error": "error occurred", "source": "test"}

    def test_with_symbol(self):
        result = shared.error_payload(
            source="upbit", message="not found", symbol="KRW-BTC"
        )
        assert result == {
            "error": "not found",
            "source": "upbit",
            "symbol": "KRW-BTC",
        }

    def test_with_all_fields(self):
        result = shared.error_payload(
            source="yahoo",
            message="API error",
            symbol="AAPL",
            instrument_type="equity_us",
            query="search query",
        )
        assert result == {
            "error": "API error",
            "source": "yahoo",
            "symbol": "AAPL",
            "instrument_type": "equity_us",
            "query": "search query",
        }

    def test_none_values_excluded(self):
        result = shared.error_payload(
            source="kis", message="error", symbol=None, instrument_type=None
        )
        assert "symbol" not in result
        assert "instrument_type" not in result


# ---------------------------------------------------------------------------
# Row Normalization Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestNormalizeRows:
    """Tests for normalize_rows helper function."""

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        assert shared.normalize_rows(df) == []

    def test_single_row(self):
        df = pd.DataFrame([{"a": 1, "b": "text"}])
        result = shared.normalize_rows(df)
        assert result == [{"a": 1, "b": "text"}]

    def test_multiple_rows(self):
        df = pd.DataFrame([{"x": 1}, {"x": 2}, {"x": 3}])
        result = shared.normalize_rows(df)
        assert len(result) == 3
        assert result[0]["x"] == 1
        assert result[2]["x"] == 3

    def test_normalizes_values(self):
        import datetime

        df = pd.DataFrame(
            [
                {
                    "date": datetime.date(2024, 1, 15),
                    "value": float("nan"),
                    "count": 42,
                }
            ]
        )
        result = shared.normalize_rows(df)
        assert result[0]["date"] == "2024-01-15"
        assert result[0]["value"] is None
        assert result[0]["count"] == 42


# ---------------------------------------------------------------------------
# Symbol Not Found Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSymbolNotFound:
    """Tests for symbol not found error handling."""

    @pytest.mark.asyncio
    async def test_get_quote_crypto_not_found(self, monkeypatch):
        tools = build_tools()
        # Return None for the symbol (not found)
        mock_fetch = AsyncMock(return_value={"KRW-INVALID": None})
        monkeypatch.setattr(upbit_service, "fetch_multiple_current_prices", mock_fetch)

        result = await tools["get_quote"]("KRW-INVALID")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "upbit"

    @pytest.mark.asyncio
    async def test_get_quote_korean_equity_not_found(self, monkeypatch):
        tools = build_tools()

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n):
                return pd.DataFrame()  # Empty DataFrame

        _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

        result = await tools["get_quote"]("999999")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "kis"

    @pytest.mark.asyncio
    async def test_get_quote_us_equity_not_found_raises(self, monkeypatch):
        tools = build_tools()
        mock_fetch_fast_info = AsyncMock(
            return_value={
                "symbol": "INVALID",
                "close": None,
                "previous_close": None,
                "open": None,
                "high": None,
                "low": None,
                "volume": None,
            }
        )
        monkeypatch.setattr(yahoo_service, "fetch_fast_info", mock_fetch_fast_info)

        with pytest.raises(ValueError, match="Symbol 'INVALID' not found"):
            await tools["get_quote"]("INVALID")

        mock_fetch_fast_info.assert_awaited_once_with("INVALID")


# ---------------------------------------------------------------------------
# Symbol Normalization Integration Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestSymbolNormalizationIntegration:
    """Test symbol normalization for all tools that accept int symbols."""

    @pytest.mark.asyncio
    async def test_get_quote_numeric_symbol(self, monkeypatch):
        """Test that get_quote accepts numeric Korean stock symbols."""
        tools = build_tools()

        mock_quote = {
            "symbol": "012450",
            "price": 50000,
            "instrument_type": "equity_kr",
            "source": "kis",
        }

        async def mock_fetch_quote_kr(symbol):
            return mock_quote

        _patch_runtime_attr(monkeypatch, "_fetch_quote_equity_kr", mock_fetch_quote_kr)

        # Test with integer input
        result = await tools["get_quote"](12450, market="kr")
        assert result["symbol"] == "012450"

        # Test with string input
        result = await tools["get_quote"]("12450", market="kr")
        assert result["symbol"] == "012450"

    @pytest.mark.asyncio
    async def test_get_valuation_numeric_symbol(self, monkeypatch):
        """Test that get_valuation accepts numeric Korean stock symbols."""
        tools = build_tools()

        mock_valuation = {
            "symbol": "012450",
            "name": "한화에어로스페이스",
            "current_price": 50000,
            "per": 15.0,
        }

        async def mock_fetch_valuation(code):
            return mock_valuation

        monkeypatch.setattr(naver_finance, "fetch_valuation", mock_fetch_valuation)

        # Test with integer input
        result = await tools["get_valuation"](12450, market="kr")
        assert result["symbol"] == "012450"

        # Test with string input
        result = await tools["get_valuation"]("12450", market="kr")
        assert result["symbol"] == "012450"

    @pytest.mark.asyncio
    async def test_get_news_numeric_symbol(self, monkeypatch):
        """Test that get_news accepts numeric Korean stock symbols."""
        tools = build_tools()

        mock_news = {
            "symbol": "012450",
            "count": 2,
            "news": [
                {"title": "뉴스1", "source": "연합뉴스", "datetime": "2024-01-15"},
                {"title": "뉴스2", "source": "한국경제", "datetime": "2024-01-14"},
            ],
        }

        async def mock_fetch_news(code, limit):
            return mock_news["news"]

        monkeypatch.setattr(naver_finance, "fetch_news", mock_fetch_news)

        # Test with integer input
        result = await tools["get_news"](12450, market="kr", limit=10)
        # News endpoint should normalize the symbol
        assert "012450" in str(result)

        # Test with string input
        result = await tools["get_news"]("12450", market="kr", limit=10)
        assert "012450" in str(result)
