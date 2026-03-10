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
from app.services.us_symbol_universe_service import USSymbolNotRegisteredError
from tests._mcp_tooling_support import (
    _KR_SYNC_HINT,
    _patch_runtime_attr,
    _single_row_df,
    build_tools,
)


def _single_crypto_intraday_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "datetime": pd.Timestamp("2024-01-01 09:30:00"),
                "date": date(2024, 1, 1),
                "time": datetime.time(9, 30, 0),
                "open": 100.0,
                "high": 110.0,
                "low": 90.0,
                "close": 105.0,
                "volume": 1000.0,
                "value": 105000.0,
            }
        ]
    )


_CRYPTO_MINUTE_PUBLIC_KEYS = {
    "timestamp",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "trade_amount",
}

_OHLCV_INDICATOR_KEYS = {
    "rsi_14",
    "ema_20",
    "bb_upper",
    "bb_mid",
    "bb_lower",
    "vwap",
}


def _multi_row_crypto_intraday_df(rows: int = 25) -> pd.DataFrame:
    base_timestamp = pd.Timestamp("2024-01-01 09:00:00")
    records: list[dict[str, object]] = []
    for idx in range(rows):
        timestamp = base_timestamp + pd.Timedelta(minutes=idx)
        close = 100.0 + (idx * 0.4) + (1.5 if idx % 2 == 0 else -1.0)
        open_price = close - 0.7 if idx % 2 == 0 else close + 0.5
        high = max(open_price, close) + 1.0
        low = min(open_price, close) - 1.0
        volume = 1000.0 + (idx * 25.0)
        records.append(
            {
                "datetime": timestamp,
                "date": timestamp.date(),
                "time": timestamp.time(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "value": close * volume,
            }
        )
    return pd.DataFrame(records)


def _multi_row_daily_df(rows: int = 25) -> pd.DataFrame:
    base_date = pd.Timestamp("2024-01-01")
    records: list[dict[str, object]] = []
    for idx in range(rows):
        candle_date = base_date + pd.Timedelta(days=idx)
        close = 200.0 + (idx * 0.6) + (2.0 if idx % 2 == 0 else -1.5)
        open_price = close - 0.8 if idx % 3 else close + 0.4
        high = max(open_price, close) + 1.5
        low = min(open_price, close) - 1.5
        volume = 5000.0 + (idx * 50.0)
        records.append(
            {
                "date": candle_date.date(),
                "open": open_price,
                "high": high,
                "low": low,
                "close": close,
                "volume": volume,
                "value": close * volume,
            }
        )
    return pd.DataFrame(records)


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
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m"])
async def test_get_ohlcv_crypto_minute_periods(monkeypatch, period):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=250, period=period)

    mock_fetch.assert_awaited_once_with(
        market="KRW-BTC", days=200, period=period, end_date=None
    )
    assert result["period"] == period
    assert result["count"] == 200
    assert result["instrument_type"] == "crypto"
    assert result["indicators_included"] is False
    assert set(result["rows"][0]) == _CRYPTO_MINUTE_PUBLIC_KEYS
    assert _OHLCV_INDICATOR_KEYS.isdisjoint(result["rows"][0])


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_minute_rows_do_not_expose_datetime(monkeypatch):
    tools = build_tools()
    df = _single_crypto_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1, period="1m")

    row = result["rows"][0]
    assert "datetime" not in row
    assert row["timestamp"] == "2024-01-01T09:30:00"
    assert row["trade_amount"] == row["value"]
    assert set(row) == _CRYPTO_MINUTE_PUBLIC_KEYS


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["5m", "15m", "30m"])
async def test_get_ohlcv_crypto_minute_periods_preserve_minute_public_row_shape(
    monkeypatch, period
):
    tools = build_tools()
    df = _single_crypto_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1, period=period)

    row = result["rows"][0]
    assert "datetime" not in row
    assert row["timestamp"] == "2024-01-01T09:30:00"
    assert row["trade_amount"] == row["value"]
    assert set(row) == _CRYPTO_MINUTE_PUBLIC_KEYS


@pytest.mark.asyncio
async def test_get_ohlcv_include_indicators_enriches_crypto_minute_rows(monkeypatch):
    tools = build_tools()
    df = _multi_row_crypto_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"](
        "KRW-BTC", count=25, period="1m", include_indicators=True
    )

    assert result["indicators_included"] is True
    first_row = result["rows"][0]
    last_row = result["rows"][-1]
    assert _OHLCV_INDICATOR_KEYS.issubset(first_row)
    assert _OHLCV_INDICATOR_KEYS.issubset(last_row)
    assert first_row["rsi_14"] is None
    assert first_row["ema_20"] is None
    assert first_row["bb_upper"] is None
    assert first_row["bb_mid"] is None
    assert first_row["bb_lower"] is None
    assert first_row["vwap"] is not None
    assert last_row["rsi_14"] is not None
    assert last_row["ema_20"] is not None
    assert last_row["bb_upper"] is not None
    assert last_row["bb_mid"] is not None
    assert last_row["bb_lower"] is not None
    assert last_row["vwap"] is not None


@pytest.mark.asyncio
async def test_get_ohlcv_include_indicators_daily_sets_vwap_to_none(monkeypatch):
    tools = build_tools()
    df = _multi_row_daily_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=25, include_indicators=True)

    assert result["indicators_included"] is True
    row = result["rows"][-1]
    assert _OHLCV_INDICATOR_KEYS.issubset(row)
    assert row["vwap"] is None
    assert row["ema_20"] is not None
    assert row["bb_upper"] is not None


@pytest.mark.asyncio
async def test_get_ohlcv_empty_crypto_result_preserves_indicators_flag(monkeypatch):
    tools = build_tools()
    mock_fetch = AsyncMock(return_value=pd.DataFrame())
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"](
        "KRW-BTC", count=5, period="1m", include_indicators=True
    )

    assert result == {
        "symbol": "KRW-BTC",
        "instrument_type": "crypto",
        "source": "upbit",
        "period": "1m",
        "count": 0,
        "rows": [],
        "indicators_included": True,
        "message": "No candle data available for KRW-BTC",
    }


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_minute_missing_raw_column_raises_value_error(
    monkeypatch,
):
    tools = build_tools()
    df = _single_crypto_intraday_df().drop(columns=["time"])
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    with pytest.raises(
        ValueError, match="Crypto minute OHLCV response missing columns: time"
    ):
        await tools["get_ohlcv"]("KRW-BTC", count=1, period="1m")


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_period_1h_preserves_datetime(monkeypatch):
    tools = build_tools()
    df = _single_crypto_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1, period="1h")

    row = result["rows"][0]
    assert row["datetime"] == "2024-01-01T09:30:00"
    assert row["date"] == "2024-01-01"
    assert row["time"] == "09:30:00"
    assert set(row) == {
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    }


@pytest.mark.asyncio
async def test_get_ohlcv_crypto_period_4h_preserves_datetime(monkeypatch):
    tools = build_tools()
    df = _single_crypto_intraday_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(upbit_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"]("KRW-BTC", count=1, period="4h")

    row = result["rows"][0]
    assert row["datetime"] == "2024-01-01T09:30:00"
    assert row["date"] == "2024-01-01"
    assert row["time"] == "09:30:00"
    assert set(row) == {
        "datetime",
        "date",
        "time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "value",
    }


# ============================================================================
# US Equity OHLCV Tests
# ============================================================================


@pytest.mark.asyncio
@pytest.mark.parametrize("period", ["1m", "5m", "15m", "30m", "1h"])
async def test_get_ohlcv_us_intraday_periods_use_kis_reader(monkeypatch, period):
    from app.mcp_server.tooling import market_data_quotes as mdq

    tools = build_tools()
    df = _single_row_df()
    df["session"] = "REGULAR"
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mdq, "read_us_intraday_candles", mock_fetch)

    result = await tools["get_ohlcv"]("AAPL", count=150, period=period)

    mock_fetch.assert_awaited_once()
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["symbol"] == "AAPL"
    assert call_kwargs["period"] == period
    assert call_kwargs["count"] == 100  # capped at 100 for MCP
    assert call_kwargs["end_date"] is None
    assert call_kwargs["end_date_is_date_only"] is False
    assert result["period"] == period
    assert result["instrument_type"] == "equity_us"
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_us_intraday_date_only_end_date_uses_post_market_cursor(
    monkeypatch,
):
    from app.mcp_server.tooling import market_data_quotes as mdq

    tools = build_tools()
    df = _single_row_df()
    df["session"] = "REGULAR"
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mdq, "read_us_intraday_candles", mock_fetch)

    result = await tools["get_ohlcv"](
        "AAPL",
        market="us",
        count=5,
        period="5m",
        end_date="2024-06-30",
    )

    mock_fetch.assert_awaited_once()
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["end_date"] == datetime.datetime(2024, 6, 30, 20, 0, 0)
    assert call_kwargs["end_date_is_date_only"] is True
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_us_intraday_timestamp_end_date_preserves_exact_instant(
    monkeypatch,
):
    from app.mcp_server.tooling import market_data_quotes as mdq

    tools = build_tools()
    df = _single_row_df()
    df["session"] = "REGULAR"
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(mdq, "read_us_intraday_candles", mock_fetch)

    result = await tools["get_ohlcv"](
        "AAPL",
        market="us",
        count=5,
        period="5m",
        end_date="2024-06-30T14:30:00",
    )

    mock_fetch.assert_awaited_once()
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["end_date"] == datetime.datetime(2024, 6, 30, 14, 30, 0)
    assert call_kwargs["end_date_is_date_only"] is False
    assert result["source"] == "kis"


@pytest.mark.asyncio
async def test_get_ohlcv_us_day_date_only_end_date_does_not_use_intraday_cursor(
    monkeypatch,
):
    tools = build_tools()
    df = _single_row_df()
    mock_fetch = AsyncMock(return_value=df)
    monkeypatch.setattr(yahoo_service, "fetch_ohlcv", mock_fetch)

    result = await tools["get_ohlcv"](
        "AAPL",
        market="us",
        count=5,
        period="day",
        end_date="2024-06-30",
    )

    mock_fetch.assert_awaited_once()
    call_kwargs = mock_fetch.call_args.kwargs
    assert call_kwargs["end_date"] == datetime.datetime(2024, 6, 30, 0, 0, 0)
    assert result["source"] == "yahoo"


@pytest.mark.asyncio
async def test_get_ohlcv_us_intraday_lookup_failure_returns_kis_error_payload(
    monkeypatch,
):
    from app.mcp_server.tooling import market_data_quotes as mdq

    tools = build_tools()
    read_mock = AsyncMock(
        side_effect=USSymbolNotRegisteredError("US symbol 'AAPL' is not registered")
    )
    monkeypatch.setattr(mdq, "read_us_intraday_candles", read_mock)

    result = await tools["get_ohlcv"]("AAPL", market="us", count=5, period="5m")

    assert result == {
        "error": "US symbol 'AAPL' is not registered",
        "source": "kis",
        "symbol": "AAPL",
        "instrument_type": "equity_us",
    }


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
    df = _multi_row_daily_df()
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
async def test_get_ohlcv_kr_intraday_include_indicators_preserves_fields(monkeypatch):
    tools = build_tools()
    base = pd.Timestamp("2026-02-23 09:00:00")
    df = pd.DataFrame(
        [
            {
                "datetime": current,
                "date": current.date(),
                "time": current.time(),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000.0 + (index * 10.0),
                "value": (1000.0 + (index * 10.0)) * close,
                "session": "REGULAR",
                "venues": ["KRX", "NTX"],
            }
            for index in range(25)
            for current, close in [
                (base + pd.Timedelta(minutes=index), 100.0 + (index * 0.4))
            ]
        ]
    )
    read_mock = AsyncMock(return_value=df)
    monkeypatch.setattr(market_data_quotes, "read_kr_intraday_candles", read_mock)

    result = await tools["get_ohlcv"](
        "005930",
        market="kr",
        count=25,
        period="5m",
        include_indicators=True,
    )

    assert result["indicators_included"] is True
    first_row = result["rows"][0]
    last_row = result["rows"][-1]
    assert first_row["session"] == "REGULAR"
    assert first_row["venues"] == ["KRX", "NTX"]
    assert first_row["rsi_14"] is None
    assert first_row["ema_20"] is None
    assert first_row["bb_upper"] is None
    assert first_row["bb_mid"] is None
    assert first_row["bb_lower"] is None
    assert first_row["vwap"] is not None
    assert last_row["rsi_14"] is not None
    assert last_row["ema_20"] is not None
    assert last_row["bb_upper"] is not None
    assert last_row["bb_mid"] is not None
    assert last_row["bb_lower"] is not None
    assert last_row["vwap"] is not None


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
