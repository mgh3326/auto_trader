from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.mcp_server import tools as mcp_tools


class DummyMCP:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, name: str, description: str):
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


class DummySessionManager:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, tb):
        return None


def build_tools() -> dict[str, object]:
    mcp = DummyMCP()
    mcp_tools.register_tools(mcp)
    return mcp.tools


def _single_row_df() -> pd.DataFrame:
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


@pytest.mark.asyncio
async def test_search_symbol_empty_query_returns_empty():
    tools = build_tools()

    result = await tools["search_symbol"]("   ")

    assert result == []


@pytest.mark.asyncio
async def test_search_symbol_clamps_limit_and_shapes(monkeypatch):
    tools = build_tools()

    # Mock master data
    monkeypatch.setattr(
        mcp_tools,
        "get_kospi_name_to_code",
        lambda: {"삼성전자": "005930", "삼성SDI": "006400"},
    )
    monkeypatch.setattr(mcp_tools, "get_kosdaq_name_to_code", lambda: {})
    monkeypatch.setattr(
        mcp_tools,
        "get_us_stocks_data",
        lambda: {
            "symbol_to_exchange": {},
            "symbol_to_name_kr": {},
            "symbol_to_name_en": {},
        },
    )

    result = await tools["search_symbol"]("삼성", limit=500)

    # limit should be capped at 100
    assert len(result) == 2
    assert result[0]["symbol"] == "005930"
    assert result[0]["name"] == "삼성전자"
    assert result[0]["instrument_type"] == "equity_kr"
    assert result[0]["exchange"] == "KOSPI"


@pytest.mark.asyncio
async def test_search_symbol_with_market_filter(monkeypatch):
    tools = build_tools()

    # Mock master data
    monkeypatch.setattr(
        mcp_tools,
        "get_kospi_name_to_code",
        lambda: {"애플": "123456"},
    )
    monkeypatch.setattr(mcp_tools, "get_kosdaq_name_to_code", lambda: {})
    monkeypatch.setattr(
        mcp_tools,
        "get_us_stocks_data",
        lambda: {
            "symbol_to_exchange": {"AAPL": "NASDAQ"},
            "symbol_to_name_kr": {"AAPL": "애플"},
            "symbol_to_name_en": {"AAPL": "Apple Inc."},
        },
    )

    # Search with us market filter
    result = await tools["search_symbol"]("애플", market="us")

    assert len(result) == 1
    assert result[0]["symbol"] == "AAPL"
    assert result[0]["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_search_symbol_returns_error_payload(monkeypatch):
    tools = build_tools()

    def raise_error():
        raise RuntimeError("master data failed")

    monkeypatch.setattr(mcp_tools, "get_kospi_name_to_code", raise_error)

    result = await tools["search_symbol"]("samsung")

    assert len(result) == 1
    assert result[0]["error"] == "master data failed"
    assert result[0]["source"] == "master"
    assert result[0]["query"] == "samsung"


@pytest.mark.asyncio
async def test_get_quote_crypto(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(return_value={"KRW-BTC": 123.4})
    monkeypatch.setattr(
        mcp_tools.upbit_service, "fetch_multiple_current_prices", mock_fetch
    )

    result = await tools["get_quote"]("krw-btc")

    mock_fetch.assert_awaited_once_with(["KRW-BTC"])
    assert result == {
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
        "price": 123.4,
        "source": "upbit",
    }


@pytest.mark.asyncio
async def test_get_quote_crypto_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("upbit down"))
    monkeypatch.setattr(
        mcp_tools.upbit_service, "fetch_multiple_current_prices", mock_fetch
    )

    result = await tools["get_quote"]("KRW-BTC")

    assert result == {
        "error": "upbit down",
        "source": "upbit",
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
    }


@pytest.mark.asyncio
async def test_get_quote_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == 105.0  # price = close
    assert result["open"] == 100.0


@pytest.mark.asyncio
async def test_get_quote_korean_equity_returns_error_payload(monkeypatch):
    tools = build_tools()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            raise RuntimeError("kis down")

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("005930")

    assert result == {
        "error": "kis down",
        "source": "kis",
        "symbol": "005930",
        "instrument_type": "equity_kr",
    }


@pytest.mark.asyncio
async def test_get_quote_korean_etf(monkeypatch):
    """Test get_quote with Korean ETF code (alphanumeric like 0123G0)."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("0123G0")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["price"] == 105.0


@pytest.mark.asyncio
async def test_get_quote_korean_etf_with_explicit_market(monkeypatch):
    """Test get_quote with Korean ETF code and explicit market=kr."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_quote"]("0117V0", market="kr")

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_quote_us_equity(monkeypatch):
    tools = build_tools()

    # Mock yfinance Ticker
    class MockFastInfo:
        last_price = 205.0
        regular_market_previous_close = 200.0
        open = 201.0
        day_high = 210.0
        day_low = 199.0
        last_volume = 50000000

    class MockTicker:
        fast_info = MockFastInfo()

    monkeypatch.setattr("yfinance.Ticker", lambda symbol: MockTicker())

    result = await tools["get_quote"]("AAPL")

    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["price"] == 205.0
    assert result["previous_close"] == 200.0
    assert result["open"] == 201.0
    assert result["high"] == 210.0
    assert result["low"] == 199.0
    assert result["volume"] == 50000000


@pytest.mark.asyncio
async def test_get_quote_us_equity_returns_error_payload(monkeypatch):
    tools = build_tools()

    def raise_error(symbol):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr("yfinance.Ticker", raise_error)

    result = await tools["get_quote"]("AAPL")

    assert result == {
        "error": "yahoo down",
        "source": "yahoo",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
    }


@pytest.mark.asyncio
async def test_get_quote_raises_on_invalid_symbol():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_quote"]("")

    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_quote"]("1234")


@pytest.mark.asyncio
async def test_get_quote_market_crypto_requires_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="crypto symbols must include KRW-/USDT- prefix"
    ):
        await tools["get_quote"]("BTC", market="crypto")


@pytest.mark.asyncio
async def test_get_quote_market_kr_requires_digits():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_quote"]("AAPL", market="kr")


@pytest.mark.asyncio
async def test_get_quote_market_us_rejects_crypto_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="us equity symbols must not include KRW-/USDT- prefix"
    ):
        await tools["get_quote"]("KRW-BTC", market="us")


@pytest.mark.asyncio
async def test_get_ohlcv_crypto(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=300)

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="day", end_date=None
    )
    assert result["instrument_type"] == "crypto"
    assert result["source"] == "upbit"
    assert result["count"] == 200
    assert result["period"] == "day"
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_with_period_week(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=52, period="week")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=52, period="week", end_date=None
    )
    assert result["period"] == "week"


@pytest.mark.asyncio
async def test_get_ohlcv_with_end_date(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

    await tools["get_ohlcv"]("KRW-BTC", count=100, end_date="2024-06-30")

    # Verify end_date was parsed and passed
    call_args = mock_fetch.call_args
    assert call_args.kwargs["end_date"].year == 2024
    assert call_args.kwargs["end_date"].month == 6
    assert call_args.kwargs["end_date"].day == 30


@pytest.mark.asyncio
async def test_get_ohlcv_serializes_timestamps(monkeypatch):
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "date": pd.Timestamp("2024-01-01"),
                "open": 1.0,
                "high": 2.0,
                "low": 0.5,
                "close": 1.5,
                "volume": 10,
                "value": float("nan"),
            }
        ]
    )
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1)

    row = result["rows"][0]
    assert isinstance(row["date"], str)
    assert "2024-01-01" in row["date"]
    assert row["value"] is None


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("005930", count=10)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["count"] == 10
    assert result["period"] == "day"
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity_with_period_month(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            called["period"] = period
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("005930", count=24, period="month")

    assert called["period"] == "M"  # KIS uses M for month
    assert result["period"] == "month"


@pytest.mark.asyncio
async def test_get_ohlcv_korean_etf(monkeypatch):
    """Test get_ohlcv with Korean ETF code (alphanumeric like 0123G0)."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("0123G0", count=10)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["count"] == 10


@pytest.mark.asyncio
async def test_get_ohlcv_korean_etf_with_explicit_market(monkeypatch):
    """Test get_ohlcv with Korean ETF code and explicit market=kr."""
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            return df

    monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("0117V0", market="kr", count=5)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("yahoo timeout"))
    monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=5)

    assert result == {
        "error": "yahoo timeout",
        "source": "yahoo",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
    }


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=5)

    mock_fetch.assert_awaited_once_with(
        ticker="AAPL", days=5, period="day", end_date=None
    )
    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["count"] == 5
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_input():
    tools = build_tools()

    with pytest.raises(ValueError, match="symbol is required"):
        await tools["get_ohlcv"]("")

    with pytest.raises(ValueError, match="count must be > 0"):
        await tools["get_ohlcv"]("AAPL", count=0)

    with pytest.raises(ValueError, match="Unsupported symbol format"):
        await tools["get_ohlcv"]("1234")


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_period():
    tools = build_tools()

    with pytest.raises(ValueError, match="period must be 'day', 'week', or 'month'"):
        await tools["get_ohlcv"]("AAPL", period="hour")


@pytest.mark.asyncio
async def test_get_ohlcv_raises_on_invalid_end_date():
    tools = build_tools()

    with pytest.raises(ValueError, match="end_date must be ISO format"):
        await tools["get_ohlcv"]("AAPL", end_date="invalid-date")


@pytest.mark.asyncio
async def test_get_ohlcv_market_kr_requires_digits():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="korean equity symbols must be 6 alphanumeric"
    ):
        await tools["get_ohlcv"]("AAPL", market="kr")


@pytest.mark.asyncio
async def test_get_ohlcv_market_us_rejects_crypto_prefix():
    tools = build_tools()

    with pytest.raises(
        ValueError, match="us equity symbols must not include KRW-/USDT- prefix"
    ):
        await tools["get_ohlcv"]("KRW-BTC", market="us")


@pytest.mark.unit
class TestNormalizeMarket:
    """Tests for _normalize_market helper function."""

    def test_returns_none_for_empty(self):
        assert mcp_tools._normalize_market(None) is None
        assert mcp_tools._normalize_market("") is None
        assert mcp_tools._normalize_market("   ") is None

    def test_crypto_aliases(self):
        for alias in ["crypto", "upbit", "krw", "usdt"]:
            assert mcp_tools._normalize_market(alias) == "crypto"

    def test_equity_kr_aliases(self):
        for alias in ["kr", "krx", "korea", "kospi", "kosdaq", "kis", "equity_kr"]:
            assert mcp_tools._normalize_market(alias) == "equity_kr"

    def test_equity_us_aliases(self):
        for alias in ["us", "usa", "nyse", "nasdaq", "yahoo", "equity_us"]:
            assert mcp_tools._normalize_market(alias) == "equity_us"

    def test_case_insensitive(self):
        assert mcp_tools._normalize_market("CRYPTO") == "crypto"
        assert mcp_tools._normalize_market("KR") == "equity_kr"
        assert mcp_tools._normalize_market("Us") == "equity_us"

    def test_unknown_returns_none(self):
        assert mcp_tools._normalize_market("unknown") is None
        assert mcp_tools._normalize_market("invalid") is None


@pytest.mark.unit
class TestSymbolDetection:
    """Tests for symbol detection helper functions."""

    def test_is_korean_equity_code(self):
        # Regular stocks (6 digits)
        assert mcp_tools._is_korean_equity_code("005930") is True
        assert mcp_tools._is_korean_equity_code("000660") is True
        assert mcp_tools._is_korean_equity_code("  005930  ") is True
        # ETF/ETN (6 alphanumeric)
        assert mcp_tools._is_korean_equity_code("0123G0") is True  # ETF
        assert mcp_tools._is_korean_equity_code("0117V0") is True  # ETF
        assert mcp_tools._is_korean_equity_code("12345A") is True  # alphanumeric
        assert mcp_tools._is_korean_equity_code("0123g0") is True  # lowercase
        # Invalid codes
        assert mcp_tools._is_korean_equity_code("00593") is False  # 5 chars
        assert mcp_tools._is_korean_equity_code("0059300") is False  # 7 chars
        assert mcp_tools._is_korean_equity_code("AAPL") is False  # 4 chars
        assert mcp_tools._is_korean_equity_code("0123-0") is False  # contains hyphen

    def test_is_crypto_market(self):
        assert mcp_tools._is_crypto_market("KRW-BTC") is True
        assert mcp_tools._is_crypto_market("krw-btc") is True
        assert mcp_tools._is_crypto_market("USDT-BTC") is True
        assert mcp_tools._is_crypto_market("usdt-eth") is True
        assert mcp_tools._is_crypto_market("BTC") is False
        assert mcp_tools._is_crypto_market("AAPL") is False
        assert mcp_tools._is_crypto_market("005930") is False

    def test_is_us_equity_symbol(self):
        assert mcp_tools._is_us_equity_symbol("AAPL") is True
        assert mcp_tools._is_us_equity_symbol("MSFT") is True
        assert mcp_tools._is_us_equity_symbol("BRK.B") is True
        assert mcp_tools._is_us_equity_symbol("KRW-BTC") is False  # crypto prefix
        assert mcp_tools._is_us_equity_symbol("005930") is False  # all digits


@pytest.mark.unit
class TestNormalizeValue:
    """Tests for _normalize_value helper function."""

    def test_none_returns_none(self):
        assert mcp_tools._normalize_value(None) is None

    def test_nan_returns_none(self):
        import numpy as np

        assert mcp_tools._normalize_value(float("nan")) is None
        assert mcp_tools._normalize_value(np.nan) is None

    def test_datetime_returns_isoformat(self):
        import datetime

        dt = datetime.datetime(2024, 1, 15, 10, 30, 0)
        assert mcp_tools._normalize_value(dt) == "2024-01-15T10:30:00"

        d = datetime.date(2024, 1, 15)
        assert mcp_tools._normalize_value(d) == "2024-01-15"

    def test_timedelta_returns_seconds(self):
        td = pd.Timedelta(hours=1, minutes=30)
        assert mcp_tools._normalize_value(td) == 5400.0

    def test_numpy_scalar_returns_python_type(self):
        import numpy as np

        assert mcp_tools._normalize_value(np.int64(42)) == 42
        assert mcp_tools._normalize_value(np.float64(3.14)) == 3.14

    def test_regular_values_pass_through(self):
        assert mcp_tools._normalize_value(42) == 42
        assert mcp_tools._normalize_value(3.14) == 3.14
        assert mcp_tools._normalize_value("hello") == "hello"


@pytest.mark.unit
class TestResolveMarketType:
    """Tests for _resolve_market_type helper function."""

    def test_explicit_crypto_normalizes_symbol(self):
        market_type, symbol = mcp_tools._resolve_market_type("krw-btc", "crypto")
        assert market_type == "crypto"
        assert symbol == "KRW-BTC"

    def test_explicit_crypto_rejects_invalid_prefix(self):
        with pytest.raises(ValueError, match="KRW-/USDT- prefix"):
            mcp_tools._resolve_market_type("BTC", "crypto")

    def test_explicit_equity_kr_validates_digits(self):
        market_type, symbol = mcp_tools._resolve_market_type("005930", "kr")
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_explicit_equity_kr_validates_etf(self):
        """Test explicit market=kr with ETF alphanumeric code."""
        market_type, symbol = mcp_tools._resolve_market_type("0123G0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_explicit_equity_kr_validates_etf_lowercase(self):
        """Test explicit market=kr with lowercase ETF code (should be accepted)."""
        market_type, symbol = mcp_tools._resolve_market_type("0123g0", "kr")
        assert market_type == "equity_kr"
        assert symbol == "0123g0"

    def test_explicit_equity_kr_rejects_invalid_format(self):
        with pytest.raises(ValueError, match="6 alphanumeric"):
            mcp_tools._resolve_market_type("AAPL", "kr")

    def test_explicit_equity_us_rejects_crypto_prefix(self):
        with pytest.raises(ValueError, match="must not include KRW-/USDT-"):
            mcp_tools._resolve_market_type("KRW-BTC", "us")

    def test_auto_detect_crypto(self):
        market_type, symbol = mcp_tools._resolve_market_type("krw-eth", None)
        assert market_type == "crypto"
        assert symbol == "KRW-ETH"

    def test_auto_detect_korean_equity(self):
        market_type, symbol = mcp_tools._resolve_market_type("005930", None)
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_auto_detect_korean_etf(self):
        """Test auto-detection of Korean ETF code (alphanumeric)."""
        market_type, symbol = mcp_tools._resolve_market_type("0123G0", None)
        assert market_type == "equity_kr"
        assert symbol == "0123G0"

    def test_auto_detect_korean_etf_another(self):
        """Test auto-detection with another ETF code pattern."""
        market_type, symbol = mcp_tools._resolve_market_type("0117V0", None)
        assert market_type == "equity_kr"
        assert symbol == "0117V0"

    def test_auto_detect_us_equity(self):
        market_type, symbol = mcp_tools._resolve_market_type("AAPL", None)
        assert market_type == "equity_us"
        assert symbol == "AAPL"

    def test_unsupported_symbol_raises(self):
        with pytest.raises(ValueError, match="Unsupported symbol format"):
            mcp_tools._resolve_market_type("1234", None)

    def test_market_aliases(self):
        # Test various market aliases
        assert mcp_tools._resolve_market_type("KRW-BTC", "upbit")[0] == "crypto"
        assert mcp_tools._resolve_market_type("005930", "kospi")[0] == "equity_kr"
        assert mcp_tools._resolve_market_type("AAPL", "nasdaq")[0] == "equity_us"


@pytest.mark.unit
class TestErrorPayload:
    """Tests for _error_payload helper function."""

    def test_minimal_payload(self):
        result = mcp_tools._error_payload(source="test", message="error occurred")
        assert result == {"error": "error occurred", "source": "test"}

    def test_with_symbol(self):
        result = mcp_tools._error_payload(
            source="upbit", message="not found", symbol="KRW-BTC"
        )
        assert result == {
            "error": "not found",
            "source": "upbit",
            "symbol": "KRW-BTC",
        }

    def test_with_all_fields(self):
        result = mcp_tools._error_payload(
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
        result = mcp_tools._error_payload(
            source="kis", message="error", symbol=None, instrument_type=None
        )
        assert "symbol" not in result
        assert "instrument_type" not in result


@pytest.mark.unit
class TestNormalizeRows:
    """Tests for _normalize_rows helper function."""

    def test_empty_dataframe(self):
        df = pd.DataFrame()
        assert mcp_tools._normalize_rows(df) == []

    def test_single_row(self):
        df = pd.DataFrame([{"a": 1, "b": "text"}])
        result = mcp_tools._normalize_rows(df)
        assert result == [{"a": 1, "b": "text"}]

    def test_multiple_rows(self):
        df = pd.DataFrame([{"x": 1}, {"x": 2}, {"x": 3}])
        result = mcp_tools._normalize_rows(df)
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
        result = mcp_tools._normalize_rows(df)
        assert result[0]["date"] == "2024-01-15"
        assert result[0]["value"] is None
        assert result[0]["count"] == 42


@pytest.mark.unit
class TestSymbolNotFound:
    """Tests for symbol not found error handling."""

    @pytest.mark.asyncio
    async def test_get_quote_crypto_not_found(self, monkeypatch):
        tools = build_tools()
        # Return None for the symbol (not found)
        mock_fetch = AsyncMock(return_value={"KRW-INVALID": None})
        monkeypatch.setattr(
            mcp_tools.upbit_service, "fetch_multiple_current_prices", mock_fetch
        )

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

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await tools["get_quote"]("999999")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "kis"

    @pytest.mark.asyncio
    async def test_get_quote_us_equity_not_found(self, monkeypatch):
        tools = build_tools()

        # Mock yfinance Ticker with None values (invalid symbol)
        class MockFastInfo:
            last_price = None
            regular_market_previous_close = None
            open = None
            day_high = None
            day_low = None
            last_volume = None

        class MockTicker:
            fast_info = MockFastInfo()

        monkeypatch.setattr("yfinance.Ticker", lambda symbol: MockTicker())

        result = await tools["get_quote"]("INVALID")

        assert "error" in result
        assert "not found" in result["error"].lower()
        assert result["source"] == "yahoo"


# ---------------------------------------------------------------------------
# Technical Indicator Tests
# ---------------------------------------------------------------------------


def _sample_ohlcv_df(n: int = 250, include_date: bool = True) -> pd.DataFrame:
    """Create sample OHLCV DataFrame for indicator testing."""
    import datetime as dt

    import numpy as np

    np.random.seed(42)
    base_price = 100.0
    prices = base_price + np.cumsum(np.random.randn(n) * 2)

    df = pd.DataFrame(
        {
            "open": prices + np.random.randn(n) * 0.5,
            "high": prices + abs(np.random.randn(n) * 1.5),
            "low": prices - abs(np.random.randn(n) * 1.5),
            "close": prices,
            "volume": np.random.randint(1000, 10000, n),
        }
    )

    if include_date:
        # Generate dates going back from today
        end_date = dt.date.today()
        dates = [end_date - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]
        df["date"] = dates

    return df


@pytest.mark.unit
class TestCalculateSMA:
    """Tests for _calculate_sma function."""

    def test_calculates_sma_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = mcp_tools._calculate_sma(df["close"])

        assert "5" in result
        assert "20" in result
        assert "60" in result
        assert "120" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_sma(df["close"])

        assert result["5"] is not None
        assert result["20"] is None
        assert result["200"] is None

    def test_custom_periods(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_sma(df["close"], periods=[5, 10, 25])

        assert "5" in result
        assert "10" in result
        assert "25" in result
        assert len(result) == 3


@pytest.mark.unit
class TestCalculateEMA:
    """Tests for _calculate_ema function."""

    def test_calculates_ema_for_all_periods(self):
        df = _sample_ohlcv_df(250)
        result = mcp_tools._calculate_ema(df["close"])

        assert "5" in result
        assert "20" in result
        assert "200" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_ema(df["close"])

        assert result["5"] is not None
        assert result["20"] is None

    def test_ema_differs_from_sma(self):
        df = _sample_ohlcv_df(50)
        sma = mcp_tools._calculate_sma(df["close"], periods=[20])
        ema = mcp_tools._calculate_ema(df["close"], periods=[20])

        # EMA gives more weight to recent prices, so values should differ
        assert sma["20"] != ema["20"]


@pytest.mark.unit
class TestCalculateRSI:
    """Tests for _calculate_rsi function."""

    def test_calculates_rsi(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_rsi(df["close"])

        assert "14" in result
        assert result["14"] is not None
        # RSI should be between 0 and 100
        assert 0 <= result["14"] <= 100

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_rsi(df["close"])

        assert result["14"] is None

    def test_custom_period(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_rsi(df["close"], period=7)

        assert "7" in result
        assert result["7"] is not None


@pytest.mark.unit
class TestCalculateMACD:
    """Tests for _calculate_macd function."""

    def test_calculates_macd(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_macd(df["close"])

        assert "macd" in result
        assert "signal" in result
        assert "histogram" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(20)
        result = mcp_tools._calculate_macd(df["close"])

        assert result["macd"] is None
        assert result["signal"] is None
        assert result["histogram"] is None

    def test_histogram_equals_macd_minus_signal(self):
        df = _sample_ohlcv_df(100)
        result = mcp_tools._calculate_macd(df["close"])

        expected_hist = result["macd"] - result["signal"]
        assert abs(result["histogram"] - expected_hist) < 0.01


@pytest.mark.unit
class TestCalculateBollinger:
    """Tests for _calculate_bollinger function."""

    def test_calculates_bollinger_bands(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_bollinger(df["close"])

        assert "upper" in result
        assert "middle" in result
        assert "lower" in result
        assert all(v is not None for v in result.values())
        # Upper > middle > lower
        assert result["upper"] > result["middle"] > result["lower"]

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_bollinger(df["close"])

        assert result["upper"] is None
        assert result["middle"] is None
        assert result["lower"] is None

    def test_middle_equals_sma(self):
        df = _sample_ohlcv_df(50)
        bollinger = mcp_tools._calculate_bollinger(df["close"], period=20)
        sma = mcp_tools._calculate_sma(df["close"], periods=[20])

        assert abs(bollinger["middle"] - sma["20"]) < 0.01


@pytest.mark.unit
class TestCalculateATR:
    """Tests for _calculate_atr function."""

    def test_calculates_atr(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_atr(df["high"], df["low"], df["close"])

        assert "14" in result
        assert result["14"] is not None
        assert result["14"] > 0

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(10)
        result = mcp_tools._calculate_atr(df["high"], df["low"], df["close"])

        assert result["14"] is None


@pytest.mark.unit
class TestCalculatePivot:
    """Tests for _calculate_pivot function."""

    def test_calculates_pivot_points(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_pivot(df["high"], df["low"], df["close"])

        assert "p" in result
        assert "r1" in result
        assert "r2" in result
        assert "r3" in result
        assert "s1" in result
        assert "s2" in result
        assert "s3" in result
        assert all(v is not None for v in result.values())

    def test_returns_none_for_insufficient_data(self):
        df = _sample_ohlcv_df(1)
        result = mcp_tools._calculate_pivot(df["high"], df["low"], df["close"])

        assert result["p"] is None
        assert result["r1"] is None
        assert result["s1"] is None

    def test_pivot_ordering(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._calculate_pivot(df["high"], df["low"], df["close"])

        # R3 > R2 > R1 > P > S1 > S2 > S3
        assert result["r3"] > result["r2"] > result["r1"]
        assert result["s1"] > result["s2"] > result["s3"]


@pytest.mark.unit
class TestComputeIndicators:
    """Tests for _compute_indicators function."""

    def test_computes_single_indicator(self):
        df = _sample_ohlcv_df(50)
        result = mcp_tools._compute_indicators(df, ["rsi"])

        assert "rsi" in result
        assert len(result) == 1

    def test_computes_multiple_indicators(self):
        df = _sample_ohlcv_df(100)
        result = mcp_tools._compute_indicators(df, ["sma", "ema", "rsi", "macd"])

        assert "sma" in result
        assert "ema" in result
        assert "rsi" in result
        assert "macd" in result

    def test_computes_all_indicators(self):
        df = _sample_ohlcv_df(250)
        all_indicators = ["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"]
        result = mcp_tools._compute_indicators(df, all_indicators)

        for indicator in all_indicators:
            assert indicator in result

    def test_raises_on_missing_columns(self):
        df = pd.DataFrame({"close": [1, 2, 3]})

        with pytest.raises(ValueError, match="Missing required columns"):
            mcp_tools._compute_indicators(df, ["atr"])


@pytest.mark.asyncio
class TestGetIndicatorsTool:
    """Tests for get_indicators tool."""

    async def test_returns_indicators(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi", "macd"])

        assert result["symbol"] == "KRW-BTC"
        assert result["instrument_type"] == "crypto"
        assert result["source"] == "upbit"
        assert "price" in result
        assert "indicators" in result
        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]

    async def test_raises_on_empty_symbol(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_indicators"]("", ["rsi"])

    async def test_raises_on_empty_indicators(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="indicators list is required"):
            await tools["get_indicators"]("KRW-BTC", [])

    async def test_raises_on_invalid_indicator(self):
        tools = build_tools()

        with pytest.raises(ValueError, match="Invalid indicator 'invalid'"):
            await tools["get_indicators"]("KRW-BTC", ["invalid"])

    async def test_returns_error_payload_on_failure(self, monkeypatch):
        tools = build_tools()
        mock_fetch = AsyncMock(side_effect=RuntimeError("API error"))
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi"])

        assert "error" in result
        assert result["source"] == "upbit"

    async def test_korean_equity(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n, period):
                return df

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await tools["get_indicators"]("005930", ["sma", "bollinger"])

        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "kis"
        assert "sma" in result["indicators"]
        assert "bollinger" in result["indicators"]

    async def test_korean_etf(self, monkeypatch):
        """Test get_indicators with Korean ETF code (alphanumeric like 0123G0)."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n, period):
                return df

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await tools["get_indicators"]("0123G0", ["rsi", "macd"])

        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "kis"
        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]

    async def test_us_equity(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("AAPL", ["ema", "atr", "pivot"])

        assert result["instrument_type"] == "equity_us"
        assert result["source"] == "yahoo"
        assert "ema" in result["indicators"]
        assert "atr" in result["indicators"]
        assert "pivot" in result["indicators"]

    async def test_case_insensitive_indicators(self, monkeypatch):
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["RSI", "MACD", "Sma"])

        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]
        assert "sma" in result["indicators"]

    async def test_all_indicators_at_once(self, monkeypatch):
        """Test requesting all indicators in a single call."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"](
            "KRW-BTC",
            ["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"],
        )

        assert "error" not in result
        assert len(result["indicators"]) == 7
        for ind in ["sma", "ema", "rsi", "macd", "bollinger", "atr", "pivot"]:
            assert ind in result["indicators"]

    async def test_empty_dataframe_returns_error(self, monkeypatch):
        """Test that empty DataFrame returns error payload."""
        tools = build_tools()
        mock_fetch = AsyncMock(return_value=pd.DataFrame())
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi"])

        assert "error" in result
        assert "No data available" in result["error"]

    async def test_whitespace_in_indicators(self, monkeypatch):
        """Test that whitespace in indicator names is handled."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["  rsi  ", " macd "])

        assert "rsi" in result["indicators"]
        assert "macd" in result["indicators"]

    async def test_duplicate_indicators(self, monkeypatch):
        """Test that duplicate indicators don't cause issues."""
        tools = build_tools()
        df = _sample_ohlcv_df(250)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi", "RSI", "rsi"])

        assert "error" not in result
        assert "rsi" in result["indicators"]

    async def test_price_included_in_response(self, monkeypatch):
        """Test that current price is included in response."""
        tools = build_tools()
        df = _sample_ohlcv_df(50)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await tools["get_indicators"]("KRW-BTC", ["rsi"])

        assert "price" in result
        assert result["price"] is not None
        assert isinstance(result["price"], float)


@pytest.mark.unit
class TestFetchOhlcvForIndicators:
    """Tests for _fetch_ohlcv_for_indicators helper function."""

    @pytest.mark.asyncio
    async def test_crypto_fetch_single_batch(self, monkeypatch):
        """Test crypto fetch with count <= 200 (single batch)."""
        df = _sample_ohlcv_df(100, include_date=True)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 100)

        mock_fetch.assert_awaited_once_with(
            market="KRW-BTC", days=100, period="day", end_date=None
        )
        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_crypto_pagination_multiple_batches(self, monkeypatch):
        """Test crypto fetch with count > 200 (requires pagination)."""
        import datetime

        # First batch: most recent 200 days
        df1 = _sample_ohlcv_df(200, include_date=True)
        # Second batch: next 50 days (older)
        df2 = _sample_ohlcv_df(50, include_date=True)
        # Adjust df2 dates to be older than df1
        earliest_date = df1["date"].min()
        df2["date"] = [
            earliest_date - datetime.timedelta(days=i + 1)
            for i in range(len(df2) - 1, -1, -1)
        ]

        call_count = 0

        async def mock_fetch(market, days, period, end_date=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return df1
            else:
                return df2

        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 250)

        assert call_count == 2
        assert len(result) == 250  # 200 + 50

    @pytest.mark.asyncio
    async def test_crypto_pagination_handles_empty_batch(self, monkeypatch):
        """Test that pagination stops when an empty batch is returned."""
        df = _sample_ohlcv_df(100, include_date=True)
        call_count = 0

        async def mock_fetch(market, days, period, end_date=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return df
            else:
                return pd.DataFrame()  # Empty batch

        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 250)

        assert call_count == 2
        assert len(result) == 100  # Only first batch

    @pytest.mark.asyncio
    async def test_crypto_pagination_removes_duplicates(self, monkeypatch):
        """Test that pagination properly removes duplicate dates."""
        df1 = _sample_ohlcv_df(100, include_date=True)
        df2 = _sample_ohlcv_df(50, include_date=True)
        # Make df2 have some overlapping dates with df1
        df2["date"] = df1["date"].iloc[:50].values

        call_count = 0

        async def mock_fetch(market, days, period, end_date=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return df1
            else:
                return df2

        monkeypatch.setattr(mcp_tools.upbit_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("KRW-BTC", "crypto", 150)

        # Should have 100 unique dates (duplicates removed)
        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_equity_kr_fetch(self, monkeypatch):
        df = _sample_ohlcv_df(100, include_date=True)

        class DummyKISClient:
            async def inquire_daily_itemchartprice(self, code, market, n, period):
                return df

        monkeypatch.setattr(mcp_tools, "KISClient", DummyKISClient)

        result = await mcp_tools._fetch_ohlcv_for_indicators("005930", "equity_kr", 100)

        assert len(result) == 100

    @pytest.mark.asyncio
    async def test_equity_us_fetch(self, monkeypatch):
        df = _sample_ohlcv_df(100, include_date=True)
        mock_fetch = AsyncMock(return_value=df)
        monkeypatch.setattr(mcp_tools.yahoo_service, "fetch_ohlcv", mock_fetch)

        result = await mcp_tools._fetch_ohlcv_for_indicators("AAPL", "equity_us", 100)

        mock_fetch.assert_awaited_once_with(ticker="AAPL", days=100, period="day")
        assert len(result) == 100


@pytest.mark.unit
class TestIndicatorEdgeCases:
    """Edge case tests for indicator calculations."""

    def test_sma_with_nan_values(self):
        """Test SMA handles NaN values gracefully."""
        import numpy as np

        close = pd.Series([100.0, np.nan, 102.0, 103.0, 104.0, 105.0])
        result = mcp_tools._calculate_sma(close, periods=[3])

        # pandas handles NaN in mean calculation
        assert "3" in result

    def test_ema_all_same_values(self):
        """Test EMA with constant prices."""
        close = pd.Series([100.0] * 50)
        result = mcp_tools._calculate_ema(close, periods=[20])

        # EMA of constant should equal the constant
        assert result["20"] == 100.0

    def test_rsi_strong_uptrend(self):
        """Test RSI with strong upward price movement."""
        # Create data with mostly gains and occasional small losses
        # Pattern: +2, +2, +2, -1, +2, +2, +2, -1, ... (net positive)
        prices = [100.0]
        for i in range(50):
            if (i + 1) % 4 == 0:
                prices.append(prices[-1] - 1.0)  # Small loss every 4th day
            else:
                prices.append(prices[-1] + 2.0)  # Gain most days
        close = pd.Series(prices)
        result = mcp_tools._calculate_rsi(close)

        # Strong uptrend with 3:1 gain ratio should have high RSI
        assert result["14"] is not None
        assert result["14"] > 70

    def test_rsi_all_losses(self):
        """Test RSI with only downward price movement."""
        close = pd.Series(range(100, 1, -1))  # 100, 99, 98, ... 2
        result = mcp_tools._calculate_rsi(close.astype(float))

        # All losses, no gains -> RSI should be 0
        assert result["14"] == 0.0

    def test_macd_with_trend(self):
        """Test MACD detects upward trend."""
        # Create upward trending data
        close = pd.Series([100 + i * 0.5 for i in range(100)])
        result = mcp_tools._calculate_macd(close)

        # In uptrend, fast EMA > slow EMA, so MACD should be positive
        assert result["macd"] is not None
        assert result["macd"] > 0

    def test_bollinger_width_increases_with_volatility(self):
        """Test Bollinger band width reflects volatility."""
        # Low volatility
        close_low_vol = pd.Series([100.0 + (i % 2) * 0.1 for i in range(50)])
        result_low = mcp_tools._calculate_bollinger(close_low_vol)

        # High volatility
        close_high_vol = pd.Series([100.0 + (i % 2) * 5.0 for i in range(50)])
        result_high = mcp_tools._calculate_bollinger(close_high_vol)

        low_width = result_low["upper"] - result_low["lower"]
        high_width = result_high["upper"] - result_high["lower"]

        assert high_width > low_width

    def test_atr_reflects_volatility(self):
        """Test ATR increases with larger price ranges."""
        # Low volatility
        df_low = pd.DataFrame(
            {
                "high": [101.0] * 50,
                "low": [99.0] * 50,
                "close": [100.0] * 50,
            }
        )
        result_low = mcp_tools._calculate_atr(
            df_low["high"], df_low["low"], df_low["close"]
        )

        # High volatility
        df_high = pd.DataFrame(
            {
                "high": [110.0] * 50,
                "low": [90.0] * 50,
                "close": [100.0] * 50,
            }
        )
        result_high = mcp_tools._calculate_atr(
            df_high["high"], df_high["low"], df_high["close"]
        )

        assert result_high["14"] > result_low["14"]

    def test_pivot_formula_verification(self):
        """Verify pivot point formula correctness."""
        high = pd.Series([110.0, 115.0])
        low = pd.Series([90.0, 95.0])
        close = pd.Series([100.0, 105.0])

        result = mcp_tools._calculate_pivot(high, low, close)

        # Using previous day's data (index -2): H=110, L=90, C=100
        expected_p = (110 + 90 + 100) / 3  # 100
        expected_r1 = 2 * expected_p - 90  # 110
        expected_s1 = 2 * expected_p - 110  # 90

        assert abs(result["p"] - expected_p) < 0.01
        assert abs(result["r1"] - expected_r1) < 0.01
        assert abs(result["s1"] - expected_s1) < 0.01

    def test_compute_indicators_with_minimal_data(self):
        """Test compute_indicators with just enough data."""
        df = _sample_ohlcv_df(5)
        result = mcp_tools._compute_indicators(df, ["sma", "rsi", "macd"])

        # SMA(5) should work, but RSI(14) and MACD should return None
        assert result["sma"]["5"] is not None
        assert result["rsi"]["14"] is None
        assert result["macd"]["macd"] is None


@pytest.mark.unit
class TestIndicatorDefaultConstants:
    """Test that default constants are properly defined."""

    def test_default_sma_periods(self):
        assert mcp_tools.DEFAULT_SMA_PERIODS == [5, 20, 60, 120, 200]

    def test_default_ema_periods(self):
        assert mcp_tools.DEFAULT_EMA_PERIODS == [5, 20, 60, 120, 200]

    def test_default_rsi_period(self):
        assert mcp_tools.DEFAULT_RSI_PERIOD == 14

    def test_default_macd_params(self):
        assert mcp_tools.DEFAULT_MACD_FAST == 12
        assert mcp_tools.DEFAULT_MACD_SLOW == 26
        assert mcp_tools.DEFAULT_MACD_SIGNAL == 9

    def test_default_bollinger_params(self):
        assert mcp_tools.DEFAULT_BOLLINGER_PERIOD == 20
        assert mcp_tools.DEFAULT_BOLLINGER_STD == 2.0

    def test_default_atr_period(self):
        assert mcp_tools.DEFAULT_ATR_PERIOD == 14


# ---------------------------------------------------------------------------
# Finnhub Tools Tests
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestGetFinnhubClient:
    """Test Finnhub client initialization."""

    def test_missing_api_key_raises_error(self, monkeypatch):
        """Test that missing API key raises ValueError."""
        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", None)

        with pytest.raises(ValueError, match="FINNHUB_API_KEY"):
            mcp_tools._get_finnhub_client()

    def test_returns_client_with_valid_key(self, monkeypatch):
        """Test that valid API key returns client."""
        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")

        client = mcp_tools._get_finnhub_client()

        assert client is not None


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetNews:
    """Test get_news tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_news"]("")

    async def test_invalid_market_raises_error(self):
        """Test that invalid market raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="market must be"):
            await tools["get_news"]("AAPL", market="invalid")

    async def test_us_news_success(self, monkeypatch):
        """Test successful US news fetch."""
        tools = build_tools()

        mock_news = [
            {
                "headline": "Apple announces new product",
                "source": "Bloomberg",
                "datetime": 1704067200,  # 2024-01-01
                "url": "https://example.com/news",
                "summary": "Apple released...",
                "sentiment": 0.5,
                "related": "AAPL",
            }
        ]

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_news(self, symbol, _from, to):
                return mock_news

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("AAPL", market="us", limit=5)

        assert result["symbol"] == "AAPL"
        assert result["market"] == "us"
        assert result["source"] == "finnhub"
        assert result["count"] == 1
        assert result["news"][0]["title"] == "Apple announces new product"

    async def test_crypto_news_success(self, monkeypatch):
        """Test successful crypto news fetch."""
        tools = build_tools()

        mock_news = [
            {
                "headline": "Bitcoin reaches new high",
                "source": "CoinDesk",
                "datetime": 1704067200,
                "url": "https://example.com/crypto",
                "summary": "BTC surged...",
            }
        ]

        class MockClient:
            def __init__(self, api_key):
                pass

            def general_news(self, category, min_id):
                return mock_news

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("BTC", market="crypto", limit=5)

        assert result["market"] == "crypto"
        assert result["news"][0]["title"] == "Bitcoin reaches new high"

    async def test_returns_error_payload_on_failure(self, monkeypatch):
        """Test that API errors return error payload."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_news(self, symbol, _from, to):
                raise RuntimeError("API error")

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("AAPL", market="us")

        assert "error" in result
        assert result["source"] == "finnhub"

    async def test_limit_capped_at_50(self, monkeypatch):
        """Test that limit is capped at 50."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_news(self, symbol, _from, to):
                return [{"headline": f"News {i}"} for i in range(100)]

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_news"]("AAPL", market="us", limit=100)

        assert result["count"] <= 50


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetCompanyProfile:
    """Test get_company_profile tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_company_profile"]("")

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="not available for cryptocurrencies"):
            await tools["get_company_profile"]("KRW-BTC")

    async def test_success(self, monkeypatch):
        """Test successful company profile fetch."""
        tools = build_tools()

        mock_profile = {
            "name": "Apple Inc",
            "ticker": "AAPL",
            "country": "US",
            "currency": "USD",
            "exchange": "NASDAQ",
            "ipo": "1980-12-12",
            "marketCapitalization": 3000000,
            "shareOutstanding": 15000,
            "finnhubIndustry": "Technology",
            "weburl": "https://apple.com",
            "logo": "https://example.com/logo.png",
            "phone": "1234567890",
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_profile2(self, symbol):
                return mock_profile

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_company_profile"]("AAPL")

        assert result["symbol"] == "AAPL"
        assert result["name"] == "Apple Inc"
        assert result["sector"] == "Technology"
        assert result["market_cap"] == 3000000

    async def test_not_found_returns_error(self, monkeypatch):
        """Test that not found returns error payload."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_profile2(self, symbol):
                return {}

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_company_profile"]("INVALID")

        assert "error" in result


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetFinancials:
    """Test get_financials tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_financials"]("")

    async def test_invalid_statement_raises_error(self):
        """Test that invalid statement type raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="statement must be"):
            await tools["get_financials"]("AAPL", statement="invalid")

    async def test_invalid_freq_raises_error(self):
        """Test that invalid frequency raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="freq must be"):
            await tools["get_financials"]("AAPL", freq="invalid")

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="not available for cryptocurrencies"):
            await tools["get_financials"]("KRW-BTC")

    async def test_success(self, monkeypatch):
        """Test successful financials fetch."""
        tools = build_tools()

        mock_data = {
            "data": [
                {
                    "year": 2024,
                    "quarter": 0,
                    "filedDate": "2024-01-15",
                    "startDate": "2023-01-01",
                    "endDate": "2023-12-31",
                    "report": {
                        "ic": [
                            {"label": "Revenue", "value": 100000000},
                            {"label": "Net Income", "value": 20000000},
                        ]
                    },
                }
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def financials_reported(self, symbol, freq):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_financials"](
            "AAPL", statement="income", freq="annual"
        )

        assert result["symbol"] == "AAPL"
        assert result["statement"] == "income"
        assert result["freq"] == "annual"
        assert len(result["reports"]) == 1
        assert result["reports"][0]["data"]["Revenue"] == 100000000


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetInsiderTransactions:
    """Test get_insider_transactions tool."""

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_insider_transactions"]("")

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for US stocks"):
            await tools["get_insider_transactions"]("KRW-BTC")

    async def test_success(self, monkeypatch):
        """Test successful insider transactions fetch."""
        tools = build_tools()

        mock_data = {
            "data": [
                {
                    "name": "Tim Cook",
                    "transactionCode": "S",
                    "share": 50000,
                    "change": -50000,
                    "transactionPrice": 180.0,
                    "transactionDate": "2024-01-15",
                    "filingDate": "2024-01-17",
                }
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL", limit=10)

        assert result["symbol"] == "AAPL"
        assert result["count"] == 1
        assert result["transactions"][0]["name"] == "Tim Cook"
        assert result["transactions"][0]["shares"] == 50000
        assert result["transactions"][0]["transaction_type"] == "Sale"
        assert result["transactions"][0]["transaction_code"] == "S"
        assert result["transactions"][0]["change"] == -50000

    async def test_limit_capped_at_100(self, monkeypatch):
        """Test that limit is capped at 100."""
        tools = build_tools()

        mock_data = {"data": [{"name": f"Exec {i}"} for i in range(150)]}

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL", limit=200)

        assert result["count"] <= 100

    async def test_transaction_code_mapping(self, monkeypatch):
        """Test that transaction codes are properly mapped to readable types."""
        tools = build_tools()

        mock_data = {
            "data": [
                {"name": "Exec 1", "transactionCode": "P", "share": 1000},
                {"name": "Exec 2", "transactionCode": "A", "share": 500},
                {"name": "Exec 3", "transactionCode": "M", "share": 200},
                {
                    "name": "Exec 4",
                    "transactionCode": "X",
                    "share": 100,
                },  # Unknown code
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL", limit=10)

        assert result["transactions"][0]["transaction_type"] == "Purchase"
        assert result["transactions"][0]["transaction_code"] == "P"
        assert result["transactions"][1]["transaction_type"] == "Grant/Award"
        assert result["transactions"][1]["transaction_code"] == "A"
        assert result["transactions"][2]["transaction_type"] == "Option Exercise"
        assert result["transactions"][2]["transaction_code"] == "M"
        # Unknown code should fall back to the code itself
        assert result["transactions"][3]["transaction_type"] == "X"
        assert result["transactions"][3]["transaction_code"] == "X"

    async def test_empty_transactions(self, monkeypatch):
        """Test handling of empty insider transactions."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return {"data": []}

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("UNKNOWN")

        assert result["count"] == 0
        assert result["transactions"] == []

    async def test_no_data_in_response(self, monkeypatch):
        """Test handling when API returns no data field."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def stock_insider_transactions(self, symbol):
                return {}  # No "data" field

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_insider_transactions"]("AAPL")

        assert result["count"] == 0
        assert result["transactions"] == []


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetEarningsCalendar:
    """Test get_earnings_calendar tool."""

    async def test_crypto_symbol_raises_error(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for US stocks"):
            await tools["get_earnings_calendar"](symbol="KRW-BTC")

    async def test_korean_symbol_raises_error(self):
        """Test that Korean symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for US stocks"):
            await tools["get_earnings_calendar"](symbol="005930")

    async def test_invalid_date_format_raises_error(self):
        """Test that invalid date format raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="ISO format"):
            await tools["get_earnings_calendar"](from_date="invalid")

    async def test_success_with_symbol(self, monkeypatch):
        """Test successful earnings calendar fetch with symbol."""
        tools = build_tools()

        mock_data = {
            "earningsCalendar": [
                {
                    "symbol": "AAPL",
                    "date": "2024-01-25",
                    "hour": "amc",
                    "epsEstimate": 2.10,
                    "epsActual": 2.18,
                    "revenueEstimate": 118000000000,
                    "revenueActual": 119600000000,
                    "quarter": 1,
                    "year": 2024,
                }
            ]
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"](symbol="AAPL")

        assert result["symbol"] == "AAPL"
        assert result["count"] == 1
        assert result["earnings"][0]["eps_estimate"] == 2.10
        assert result["earnings"][0]["eps_actual"] == 2.18

    async def test_success_without_symbol(self, monkeypatch):
        """Test successful earnings calendar fetch without symbol (date range only)."""
        tools = build_tools()

        mock_data = {
            "earningsCalendar": [
                {"symbol": "AAPL", "date": "2024-01-25"},
                {"symbol": "MSFT", "date": "2024-01-30"},
            ]
        }

        captured_symbol = None

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                nonlocal captured_symbol
                captured_symbol = symbol
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"](
            from_date="2024-01-01", to_date="2024-01-31"
        )

        assert result["count"] == 2
        # Verify empty string is passed when symbol is None
        assert captured_symbol == ""

    async def test_default_dates_when_not_provided(self, monkeypatch):
        """Test that default dates are set when not provided."""
        tools = build_tools()

        mock_data = {"earningsCalendar": []}
        captured_from = None
        captured_to = None

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                nonlocal captured_from, captured_to
                captured_from = _from
                captured_to = to
                return mock_data

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"]()

        # Verify dates are set (today and 30 days from now)
        assert captured_from is not None
        assert captured_to is not None
        assert result["from_date"] == captured_from
        assert result["to_date"] == captured_to

    async def test_empty_result(self, monkeypatch):
        """Test handling of empty earnings calendar."""
        tools = build_tools()

        class MockClient:
            def __init__(self, api_key):
                pass

            def earnings_calendar(self, symbol=None, _from=None, to=None):
                return {"earningsCalendar": []}

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        result = await tools["get_earnings_calendar"](symbol="UNKNOWN")

        assert result["count"] == 0
        assert result["earnings"] == []


# ---------------------------------------------------------------------------
# Naver Finance (Korean Market) Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetNewsKorea:
    """Test get_news tool with Korean market."""

    async def test_korean_stock_news(self, monkeypatch):
        """Test fetching news for Korean stock."""
        tools = build_tools()

        mock_news = [
            {
                "title": "삼성전자, 신제품 발표",
                "source": "연합뉴스",
                "datetime": "2024-01-15",
                "url": "https://finance.naver.com/news/1",
            },
        ]

        async def mock_fetch_news(code, limit):
            return mock_news

        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_news", mock_fetch_news)

        result = await tools["get_news"]("005930", market="kr")

        assert result["symbol"] == "005930"
        assert result["source"] == "naver"
        assert result["market"] == "kr"
        assert len(result["news"]) == 1
        assert result["news"][0]["title"] == "삼성전자, 신제품 발표"

    async def test_auto_detect_korean_market(self, monkeypatch):
        """Test auto-detection of Korean market from 6-digit code."""
        tools = build_tools()

        async def mock_fetch_news(code, limit):
            return []

        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_news", mock_fetch_news)

        # Should auto-detect Korean market from 6-digit code
        result = await tools["get_news"]("005930")

        assert result["source"] == "naver"
        assert result["market"] == "kr"

    async def test_error_handling(self, monkeypatch):
        """Test error handling for Korean news fetch."""
        tools = build_tools()

        async def mock_fetch_news(code, limit):
            raise ValueError("Network error")

        monkeypatch.setattr(mcp_tools.naver_finance, "fetch_news", mock_fetch_news)

        result = await tools["get_news"]("005930", market="kr")

        assert "error" in result
        assert result["source"] == "naver"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetCompanyProfileKorea:
    """Test get_company_profile tool with Korean market."""

    async def test_korean_stock_profile(self, monkeypatch):
        """Test fetching company profile for Korean stock."""
        tools = build_tools()

        mock_profile = {
            "symbol": "005930",
            "name": "삼성전자",
            "exchange": "KOSPI",
            "sector": "전기전자",
            "market_cap": 400_0000_0000_0000,
            "per": 15.23,
            "pbr": 1.45,
        }

        async def mock_fetch_profile(code):
            return mock_profile

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_company_profile", mock_fetch_profile
        )

        result = await tools["get_company_profile"]("005930")

        assert result["symbol"] == "005930"
        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"
        assert result["name"] == "삼성전자"

    async def test_auto_detect_korean_market(self, monkeypatch):
        """Test auto-detection of Korean market from 6-digit code."""
        tools = build_tools()

        async def mock_fetch_profile(code):
            return {"symbol": code, "name": "테스트"}

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_company_profile", mock_fetch_profile
        )

        result = await tools["get_company_profile"]("005930")

        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"

    async def test_explicit_us_market_for_korean_looking_symbol(self, monkeypatch):
        """Test explicit US market override."""
        tools = build_tools()

        mock_profile = {
            "name": "US Company",
            "ticker": "123456",
        }

        class MockClient:
            def __init__(self, api_key):
                pass

            def company_profile2(self, symbol):
                return mock_profile

        monkeypatch.setattr(mcp_tools.settings, "finnhub_api_key", "test_key")
        monkeypatch.setattr(mcp_tools.finnhub, "Client", MockClient)

        # Even though 123456 looks like Korean code, explicit market=us should use Finnhub
        result = await tools["get_company_profile"]("123456", market="us")

        assert result["source"] == "finnhub"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetFinancialsKorea:
    """Test get_financials tool with Korean market."""

    async def test_korean_stock_financials(self, monkeypatch):
        """Test fetching financials for Korean stock."""
        tools = build_tools()

        mock_financials = {
            "symbol": "005930",
            "statement": "income",
            "freq": "annual",
            "currency": "KRW",
            "periods": ["2023/12", "2022/12"],
            "metrics": {
                "매출액": [300_0000_0000_0000, 280_0000_0000_0000],
                "영업이익": [50_0000_0000_0000, 45_0000_0000_0000],
            },
        }

        async def mock_fetch_financials(code, statement, freq):
            return mock_financials

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_financials", mock_fetch_financials
        )

        result = await tools["get_financials"]("005930", statement="income")

        assert result["symbol"] == "005930"
        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"
        assert "매출액" in result["metrics"]

    async def test_auto_detect_korean_market(self, monkeypatch):
        """Test auto-detection of Korean market from 6-digit code."""
        tools = build_tools()

        async def mock_fetch_financials(code, statement, freq):
            return {"symbol": code, "statement": statement, "freq": freq}

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_financials", mock_fetch_financials
        )

        result = await tools["get_financials"]("005930")

        assert result["source"] == "naver"
        assert result["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetInvestorTrends:
    """Test get_investor_trends tool."""

    async def test_success(self, monkeypatch):
        """Test successful investor trends fetch."""
        tools = build_tools()

        mock_trends = {
            "symbol": "005930",
            "days": 20,
            "data": [
                {
                    "date": "2024-01-15",
                    "close": 75000,
                    "change": 500,
                    "volume": 10000000,
                    "foreign_net": -500000,
                    "institutional_net": 1000000,
                },
                {
                    "date": "2024-01-14",
                    "close": 74500,
                    "change": -300,
                    "volume": 8000000,
                    "foreign_net": 300000,
                    "institutional_net": -200000,
                },
            ],
        }

        async def mock_fetch_trends(code, days):
            return mock_trends

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investor_trends", mock_fetch_trends
        )

        result = await tools["get_investor_trends"]("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"
        assert len(result["data"]) == 2
        assert result["data"][0]["foreign_net"] == -500000

    async def test_rejects_us_symbol(self):
        """Test that US symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for Korean stocks"):
            await tools["get_investor_trends"]("AAPL")

    async def test_rejects_crypto_symbol(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for Korean stocks"):
            await tools["get_investor_trends"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_investor_trends"]("")

    async def test_days_capped(self, monkeypatch):
        """Test that days are capped at 60."""
        tools = build_tools()

        captured_days = None

        async def mock_fetch_trends(code, days):
            nonlocal captured_days
            captured_days = days
            return {"symbol": code, "days": days, "data": []}

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investor_trends", mock_fetch_trends
        )

        await tools["get_investor_trends"]("005930", days=100)

        assert captured_days == 60


@pytest.mark.asyncio
@pytest.mark.unit
class TestGetInvestmentOpinions:
    """Test get_investment_opinions tool."""

    async def test_success(self, monkeypatch):
        """Test successful investment opinions fetch."""
        tools = build_tools()

        mock_opinions = {
            "symbol": "005930",
            "count": 2,
            "opinions": [
                {
                    "stock_name": "삼성전자",
                    "title": "반도체 업황 개선 전망",
                    "firm": "삼성증권",
                    "rating": "매수",
                    "target_price": 85000,
                    "date": "2024-01-15",
                },
                {
                    "stock_name": "삼성전자",
                    "title": "실적 호조 지속",
                    "firm": "미래에셋",
                    "rating": "Strong Buy",
                    "target_price": 90000,
                    "date": "2024-01-14",
                },
            ],
            "current_price": 75000,
            "avg_target_price": 87500,
            "max_target_price": 90000,
            "min_target_price": 85000,
            "upside_potential": 16.67,
        }

        async def mock_fetch_opinions(code, limit):
            return mock_opinions

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investment_opinions", mock_fetch_opinions
        )

        result = await tools["get_investment_opinions"]("005930", limit=10)

        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"
        assert result["source"] == "naver"
        assert result["count"] == 2
        assert result["opinions"][0]["firm"] == "삼성증권"
        # Check new target price statistics
        assert result["current_price"] == 75000
        assert result["avg_target_price"] == 87500
        assert result["max_target_price"] == 90000
        assert result["min_target_price"] == 85000
        assert result["upside_potential"] == 16.67

    async def test_rejects_us_symbol(self):
        """Test that US symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for Korean stocks"):
            await tools["get_investment_opinions"]("AAPL")

    async def test_rejects_crypto_symbol(self):
        """Test that crypto symbols are rejected."""
        tools = build_tools()

        with pytest.raises(ValueError, match="only available for Korean stocks"):
            await tools["get_investment_opinions"]("KRW-BTC")

    async def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="symbol is required"):
            await tools["get_investment_opinions"]("")

    async def test_limit_capped(self, monkeypatch):
        """Test that limit is capped at 30."""
        tools = build_tools()

        captured_limit = None

        async def mock_fetch_opinions(code, limit):
            nonlocal captured_limit
            captured_limit = limit
            return {
                "symbol": code,
                "count": 0,
                "opinions": [],
                "current_price": None,
                "avg_target_price": None,
                "max_target_price": None,
                "min_target_price": None,
                "upside_potential": None,
            }

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_investment_opinions", mock_fetch_opinions
        )

        await tools["get_investment_opinions"]("005930", limit=100)

        assert captured_limit == 30


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

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_valuation", mock_fetch_valuation
        )

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

    async def test_rejects_us_equity(self):
        """Test that US equity symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
            await tools["get_valuation"]("AAPL")

    async def test_rejects_crypto(self):
        """Test that crypto symbol raises ValueError."""
        tools = build_tools()

        with pytest.raises(ValueError, match="Korean stocks"):
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

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_valuation", mock_fetch_valuation
        )

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

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_valuation", mock_fetch_valuation
        )

        result = await tools["get_valuation"]("005930")

        assert "error" in result
        assert result["source"] == "naver"
        assert result["symbol"] == "005930"
        assert result["instrument_type"] == "equity_kr"


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
            "short_balance": {
                "balance_shares": 1_234_567,
                "balance_amount": 98_765_432_100,
                "balance_ratio": 0.5,
            },
        }

        async def mock_fetch_short_interest(code, days):
            return mock_short_interest

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("005930", days=20)

        assert result["symbol"] == "005930"
        assert result["name"] == "삼성전자"
        assert len(result["short_data"]) == 2
        assert result["short_data"][0]["date"] == "2024-01-15"
        assert result["short_data"][0]["short_amount"] == 1_000_000_000
        assert result["short_data"][0]["short_ratio"] == 5.0
        assert result["avg_short_ratio"] == 5.17
        assert result["short_balance"]["balance_shares"] == 1_234_567

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
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        await tools["get_short_interest"]("005930", days=100)

        assert captured_days == 60

    async def test_error_handling(self, monkeypatch):
        """Test error handling when fetch fails."""
        tools = build_tools()

        async def mock_fetch_short_interest(code, days):
            raise Exception("KRX API error")

        monkeypatch.setattr(
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("005930")

        assert "error" in result
        assert result["source"] == "krx"
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
            mcp_tools.naver_finance, "fetch_short_interest", mock_fetch_short_interest
        )

        result = await tools["get_short_interest"]("000000")

        assert result["symbol"] == "000000"
        assert result["short_data"] == []
        assert result["avg_short_ratio"] is None
        assert "short_balance" not in result
