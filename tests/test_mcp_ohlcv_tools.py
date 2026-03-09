"""
Tests for MCP OHLCV (Open-High-Low-Close-Volume) tools.

This module tests the get_ohlcv tool functionality including:
- Crypto OHLCV data retrieval (Upbit)
- Korean equity OHLCV data retrieval (KIS)
- US equity OHLCV data retrieval (Yahoo Finance)
- Various period support (day, week, month, 4h, 1h)
- End date handling and caching behavior
- Error handling and validation
"""

import datetime
from datetime import date
from unittest.mock import AsyncMock

import pandas as pd
import pytest

import app.services.brokers.upbit.client as upbit_service
import app.services.brokers.yahoo.client as yahoo_service
from app.core.config import settings
from app.mcp_server.tooling import market_data_quotes
from tests._mcp_tooling_support import (
    _KR_SYNC_HINT,
    _patch_runtime_attr,
    _single_row_df,
    build_tools,
)


def _multi_row_intraday_df(row_count: int = 30) -> pd.DataFrame:
    base = pd.Timestamp("2026-02-23 09:00:00")
    rows: list[dict[str, object]] = []
    for index in range(row_count):
        current = base + pd.Timedelta(minutes=index)
        close = 100.0 + ((index % 4) * 1.5) + (index * 0.1)
        rows.append(
            {
                "date": current,
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000 + (index * 10),
                "value": (1000 + (index * 10)) * close,
                "timestamp": int(current.timestamp() * 1000),
                "trade_amount": (1000 + (index * 10)) * close,
            }
        )
    return pd.DataFrame(rows)


# ============================================================================
# Crypto OHLCV Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_ohlcv_crypto(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

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
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=52, period="week")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=52, period="week", end_date=None
    )
    assert result["period"] == "week"


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_period_4h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=250, period="4h")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="4h", end_date=None
    )
    assert result["period"] == "4h"
    assert result["instrument_type"] == "crypto"


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_period_1h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=250, period="1h")

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period="1h", end_date=None
    )
    assert result["period"] == "1h"
    assert result["instrument_type"] == "crypto"


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_include_indicators_preserves_minute_row_shape(
    monkeypatch,
):
    tools = build_tools()
    df = _multi_row_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"](
        "KRW-BTC",
        count=25,
        period="1m",
        include_indicators=True,
    )

    assert result["indicators_included"] is True
    row = result["rows"][-1]
    assert row["timestamp"] is not None
    assert row["trade_amount"] is not None
    assert row["rsi_14"] is not None
    assert row["ema_20"] is not None
    assert row["bb_upper"] is not None
    assert row["bb_mid"] is not None
    assert row["bb_lower"] is not None
    assert row["vwap"] is not None


# ============================================================================
# US Equity OHLCV Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity_period_1h(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=150, period="1h")

    mock_fetch.assert_awaited_once_with(
        ticker="AAPL", days=100, period="1h", end_date=None
    )
    assert result["period"] == "1h"
    assert result["instrument_type"] == "equity_us"


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=5)

    mock_fetch.assert_awaited_once_with(
        ticker="AAPL", days=5, period="day", end_date=None
    )
    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "yahoo"
    assert result["count"] == 5
    assert len(result["rows"]) == 1


@pytest.mark.asyncio
async def test_get_ohlcv_day_include_indicators_sets_vwap_none(monkeypatch):
    tools = build_tools()
    df = _multi_row_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"](
        "AAPL",
        count=25,
        period="day",
        include_indicators=True,
    )

    assert result["indicators_included"] is True
    assert result["rows"][-1]["vwap"] is None


@pytest.mark.asyncio
async def test_get_ohlcv_us_equity_returns_error_payload(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(side_effect=RuntimeError("yahoo timeout"))
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=5)

    assert result == {
        "error": "yahoo timeout",
        "source": "yahoo",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
    }


# ============================================================================
# Korean Equity OHLCV Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called: dict[str, object] = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            called["code"] = code
            called["market"] = market
            called["n"] = n
            called["period"] = period
            called["end_date"] = end_date
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("005930", count=10)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"
    assert result["count"] == 10
    assert result["period"] == "day"
    assert len(result["rows"]) == 1
    assert called["code"] == "005930"
    assert called["market"] == "UN"
    assert called["n"] == 10
    assert called["period"] == "D"
    assert called["end_date"] is None


@pytest.mark.asyncio
async def test_get_ohlcv_korean_equity_with_period_month(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called = {}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            called["period"] = period
            return df

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

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

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

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

    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)

    result = await tools["get_ohlcv"]("0117V0", market="kr", count=5)

    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


# ============================================================================
# Korean Equity 1H Period Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_ohlcv_kr_equity_period_1h_includes_session_and_venues(monkeypatch):
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:00:00"),
                "date": date(2026, 2, 23),
                "time": datetime.time(9, 0, 0),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
                "session": "REGULAR",
                "venues": ["KRX", "NTX"],
            }
        ]
    )
    read_mock = AsyncMock(return_value=df)
    monkeypatch.setattr(market_data_quotes, "read_kr_hourly_candles_1h", read_mock)

    result = await tools["get_ohlcv"]("005930", market="kr", count=50, period="1h")

    read_mock.assert_awaited_once_with(symbol="005930", count=50, end_date=None)
    assert result["instrument_type"] == "equity_kr"
    assert result["period"] == "1h"
    assert result["source"] == "kis"
    assert result["rows"]
    row = result["rows"][0]
    assert row["session"] == "REGULAR"
    assert row["venues"] == ["KRX", "NTX"]


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m"])
async def test_get_ohlcv_kr_intraday_periods_use_shared_reader(monkeypatch, period):
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:00:00"),
                "date": date(2026, 2, 23),
                "time": datetime.time(9, 0, 0),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
                "session": "REGULAR",
                "venues": ["KRX", "NTX"],
            }
        ]
    )
    read_mock = AsyncMock(return_value=df)
    monkeypatch.setattr(market_data_quotes, "read_kr_intraday_candles", read_mock)

    result = await tools["get_ohlcv"]("005930", market="kr", count=50, period=period)

    read_mock.assert_awaited_once_with(
        symbol="005930",
        period=period,
        count=50,
        end_date=None,
    )
    assert result["instrument_type"] == "equity_kr"
    assert result["period"] == period
    assert result["source"] == "kis"
    row = result["rows"][0]
    assert row["session"] == "REGULAR"
    assert row["venues"] == ["KRX", "NTX"]


@pytest.mark.asyncio
async def test_get_ohlcv_kr_1h_does_not_use_kis_ohlcv_cache(monkeypatch):
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:00:00"),
                "date": date(2026, 2, 23),
                "time": datetime.time(9, 0, 0),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000,
                "value": 105000.0,
                "session": "REGULAR",
                "venues": ["KRX"],
            }
        ]
    )
    monkeypatch.setattr(
        market_data_quotes.kis_ohlcv_cache,
        "get_candles",
        AsyncMock(side_effect=AssertionError("KR 1h must not use kis_ohlcv_cache")),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "read_kr_hourly_candles_1h",
        AsyncMock(return_value=df),
    )

    result = await tools["get_ohlcv"]("005930", market="kr", count=1, period="1h")

    assert result["period"] == "1h"
    assert result["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
async def test_get_ohlcv_kr_5m_does_not_use_kis_ohlcv_cache(monkeypatch):
    tools = build_tools()
    df = pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2026-02-23 09:00:00"),
                "date": date(2026, 2, 23),
                "time": datetime.time(9, 0, 0),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
                "session": "REGULAR",
                "venues": ["KRX"],
            }
        ]
    )
    monkeypatch.setattr(
        market_data_quotes.kis_ohlcv_cache,
        "get_candles",
        AsyncMock(
            side_effect=AssertionError("KR intraday must not use kis_ohlcv_cache")
        ),
    )
    monkeypatch.setattr(
        market_data_quotes,
        "read_kr_intraday_candles",
        AsyncMock(return_value=df),
    )

    result = await tools["get_ohlcv"]("005930", market="kr", count=1, period="5m")

    assert result["period"] == "5m"
    assert result["instrument_type"] == "equity_kr"


@pytest.mark.asyncio
async def test_get_ohlcv_kr_1h_universe_empty_returns_error_payload(monkeypatch):
    tools = build_tools()
    monkeypatch.setattr(
        market_data_quotes,
        "read_kr_hourly_candles_1h",
        AsyncMock(
            side_effect=ValueError(
                f"kr_symbol_universe is empty. Sync required: {_KR_SYNC_HINT}"
            )
        ),
    )

    result = await tools["get_ohlcv"]("005930", market="kr", period="1h")

    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert "kr_symbol_universe is empty" in result["error"]
    assert _KR_SYNC_HINT in result["error"]


@pytest.mark.asyncio
async def test_get_ohlcv_kr_1h_unregistered_symbol_returns_error_payload(monkeypatch):
    tools = build_tools()
    monkeypatch.setattr(
        market_data_quotes,
        "read_kr_hourly_candles_1h",
        AsyncMock(
            side_effect=ValueError(
                "KR symbol '005930' is not registered in kr_symbol_universe. "
                f"Sync required: {_KR_SYNC_HINT}"
            )
        ),
    )

    result = await tools["get_ohlcv"]("005930", market="kr", period="1h")

    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert "not registered" in result["error"]
    assert _KR_SYNC_HINT in result["error"]


@pytest.mark.asyncio
async def test_get_ohlcv_kr_1h_inactive_symbol_returns_error_payload(monkeypatch):
    tools = build_tools()
    monkeypatch.setattr(
        market_data_quotes,
        "read_kr_hourly_candles_1h",
        AsyncMock(
            side_effect=ValueError(
                "KR symbol '005930' is inactive in kr_symbol_universe. "
                f"Sync required: {_KR_SYNC_HINT}"
            )
        ),
    )

    result = await tools["get_ohlcv"]("005930", market="kr", period="1h")

    assert result["source"] == "kis"
    assert result["instrument_type"] == "equity_kr"
    assert "inactive" in result["error"]
    assert _KR_SYNC_HINT in result["error"]


# ============================================================================
# End Date and Cache Tests
# ============================================================================


@pytest.mark.asyncio
async def test_get_ohlcv_with_end_date(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    await tools["get_ohlcv"]("KRW-BTC", count=100, end_date="2024-06-30")

    # Verify end_date was parsed and passed
    call_args = mock_fetch.call_args
    assert call_args.kwargs["end_date"].year == 2024
    assert call_args.kwargs["end_date"].month == 6
    assert call_args.kwargs["end_date"].day == 30


@pytest.mark.asyncio
async def test_get_ohlcv_kr_day_bypasses_cache_when_end_date_provided(monkeypatch):
    tools = build_tools()
    df = _single_row_df()
    called = {"raw": 0}

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            del code, market, n
            called["raw"] += 1
            assert period == "D"
            assert pd.Timestamp(end_date).date() == date(2024, 6, 30)
            return df

    cache_mock = AsyncMock(return_value=df)
    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(market_data_quotes.kis_ohlcv_cache, "get_candles", cache_mock)
    monkeypatch.setattr(settings, "kis_ohlcv_cache_enabled", True, raising=False)

    result = await tools["get_ohlcv"](
        "005930", market="kr", count=10, period="day", end_date="2024-06-30"
    )

    assert called["raw"] == 1
    cache_mock.assert_not_awaited()
    assert result["source"] == "kis"
    assert result["period"] == "day"


@pytest.mark.asyncio
async def test_get_ohlcv_kr_day_uses_cache_when_no_end_date(monkeypatch):
    tools = build_tools()
    df = _single_row_df()

    class DummyKISClient:
        async def inquire_daily_itemchartprice(self, code, market, n, period, end_date):
            del code, market, n, period, end_date
            raise AssertionError("raw fetch should not be called when cache hits")

    cache_mock = AsyncMock(return_value=df)
    _patch_runtime_attr(monkeypatch, "KISClient", DummyKISClient)
    monkeypatch.setattr(market_data_quotes.kis_ohlcv_cache, "get_candles", cache_mock)
    monkeypatch.setattr(settings, "kis_ohlcv_cache_enabled", True, raising=False)

    result = await tools["get_ohlcv"]("005930", market="kr", count=5, period="day")

    cache_mock.assert_awaited_once()
    await_args = cache_mock.await_args
    assert await_args is not None
    assert await_args.kwargs["symbol"] == "005930"
    assert await_args.kwargs["count"] == 5
    assert await_args.kwargs["period"] == "day"
    assert result["instrument_type"] == "equity_kr"
    assert result["source"] == "kis"


# ============================================================================
# Serialization Tests
# ============================================================================


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
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1)

    row = result["rows"][0]
    assert isinstance(row["date"], str)
    assert "2024-01-01" in row["date"]
    assert row["value"] is None


# ============================================================================
# Input Validation Tests
# ============================================================================


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

    with pytest.raises(
        ValueError,
        match="period must be 'day', 'week', 'month', '1m', '5m', '15m', '30m', '4h', or '1h'",
    ):
        await tools["get_ohlcv"]("AAPL", period="hour")


@pytest.mark.asyncio
async def test_get_ohlcv_period_4h_market_kr_rejected():
    tools = build_tools()

    with pytest.raises(ValueError, match="period '4h' is supported only for crypto"):
        await tools["get_ohlcv"]("005930", period="4h", market="kr")


@pytest.mark.asyncio
async def test_get_ohlcv_period_4h_market_us_rejected():
    tools = build_tools()

    with pytest.raises(ValueError, match="period '4h' is supported only for crypto"):
        await tools["get_ohlcv"]("AAPL", period="4h", market="us")


@pytest.mark.asyncio
async def test_get_ohlcv_period_5m_market_us_rejected():
    tools = build_tools()

    with pytest.raises(ValueError, match="period '5m' is not supported for us equity"):
        await tools["get_ohlcv"]("AAPL", period="5m", market="us")


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
