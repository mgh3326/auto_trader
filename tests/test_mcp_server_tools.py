from types import SimpleNamespace
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
        lambda: {"symbol_to_exchange": {}, "symbol_to_name_kr": {}, "symbol_to_name_en": {}},
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

    with pytest.raises(ValueError, match="korean equity symbols must be 6 digits"):
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

    result = await tools["get_ohlcv"]("KRW-BTC", count=100, end_date="2024-06-30")

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

    with pytest.raises(ValueError, match="korean equity symbols must be 6 digits"):
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
        assert mcp_tools._is_korean_equity_code("005930") is True
        assert mcp_tools._is_korean_equity_code("000660") is True
        assert mcp_tools._is_korean_equity_code("  005930  ") is True
        assert mcp_tools._is_korean_equity_code("00593") is False  # 5 digits
        assert mcp_tools._is_korean_equity_code("0059300") is False  # 7 digits
        assert mcp_tools._is_korean_equity_code("AAPL") is False
        assert mcp_tools._is_korean_equity_code("12345A") is False

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

    def test_explicit_equity_kr_rejects_non_digits(self):
        with pytest.raises(ValueError, match="6 digits"):
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
