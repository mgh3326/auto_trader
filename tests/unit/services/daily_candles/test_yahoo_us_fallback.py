import asyncio
import threading
import time
from unittest.mock import AsyncMock, patch

import pandas as pd
import pytest

from app.services import yahoo_ohlcv_cache
from app.services.brokers.yahoo import client as yahoo_client
from app.services.daily_candles.yahoo_us_fallback import (
    fetch_us_daily_yahoo_fallback,
)


class TestFetchUsDailyYahooFallback:
    @pytest.mark.asyncio
    async def test_returns_canonical_rows_with_adj_close(self):
        sample = pd.DataFrame(
            {
                "date": pd.date_range("2024-05-01", periods=3, freq="B"),
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "adj_close": [99.0, 100.0, 101.0],
                "volume": [1000, 1100, 1200],
            }
        )
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=AsyncMock(return_value=sample),
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="ILLIQUIDETF", n=3)
        assert len(rows) == 3
        assert rows[0].adj_close == pytest.approx(99.0)
        assert all(r.symbol == "ILLIQUIDETF" for r in rows)

    @pytest.mark.asyncio
    async def test_handles_missing_adj_close_column(self):
        sample = pd.DataFrame(
            {
                "date": pd.date_range("2024-05-01", periods=2, freq="B"),
                "open": [100.0, 101.0],
                "high": [101.0, 102.0],
                "low": [99.0, 100.0],
                "close": [100.5, 101.5],
                "volume": [1000, 1100],
            }
        )
        with patch(
            "app.services.brokers.yahoo.client.fetch_ohlcv",
            new=AsyncMock(return_value=sample),
        ):
            rows = await fetch_us_daily_yahoo_fallback(symbol="X", n=2)
        assert all(r.adj_close is None for r in rows)


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_external_providers")
async def test_cache_callback_accepts_keyword_arguments(monkeypatch):
    sample = pd.DataFrame({"date": [pd.Timestamp("2026-11-20").date()]})
    monkeypatch.setattr(yahoo_client.settings, "yahoo_ohlcv_cache_enabled", True)

    async def cached(ticker, *, count, period, raw_fetcher, end_date):
        assert (ticker, count, period) == ("AAPL", 5, "day")
        fetched = await raw_fetcher(
            ticker=ticker, days=count, period=period, end_date=end_date
        )
        return fetched

    get_closed_candles = AsyncMock(side_effect=cached)
    monkeypatch.setattr(yahoo_ohlcv_cache, "get_closed_candles", get_closed_candles)
    raw = AsyncMock(return_value=sample)
    monkeypatch.setattr(yahoo_client, "_fetch_ohlcv_raw", raw)
    monkeypatch.setattr(
        yahoo_client, "_filter_closed_buckets_nyse", lambda frame, _period: frame
    )

    result = await yahoo_client.fetch_ohlcv("AAPL", days=5)

    assert result.equals(sample)
    raw.assert_awaited_once_with(ticker="AAPL", days=5, period="day", end_date=None)


@pytest.mark.asyncio
@pytest.mark.usefixtures("allow_external_providers")
async def test_empty_ok_bypasses_cache_backfill(monkeypatch):
    monkeypatch.setattr(yahoo_client.settings, "yahoo_ohlcv_cache_enabled", True)
    get_closed_candles = AsyncMock(return_value=pd.DataFrame())
    monkeypatch.setattr(yahoo_ohlcv_cache, "get_closed_candles", get_closed_candles)
    empty = pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume"])
    raw = AsyncMock(return_value=empty)
    monkeypatch.setattr(yahoo_client, "_fetch_ohlcv_raw", raw)
    monkeypatch.setattr(
        yahoo_client, "_filter_closed_buckets_nyse", lambda frame, _period: frame
    )

    result = await yahoo_client.fetch_ohlcv("EMPTY", days=5, empty_ok=True)

    assert result.empty
    get_closed_candles.assert_not_awaited()
    raw.assert_awaited_once_with(
        ticker="EMPTY",
        days=5,
        period="day",
        end_date=None,
        prepost=False,
        empty_ok=True,
    )


@pytest.mark.asyncio
async def test_yfinance_sessions_are_serialized_across_download_threads(monkeypatch):
    active = 0
    max_active = 0
    guard = threading.Lock()

    def download(*_args, **_kwargs):
        nonlocal active, max_active
        with guard:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with guard:
            active -= 1
        return pd.DataFrame(
            columns=["Open", "High", "Low", "Close", "Volume"],
            index=pd.DatetimeIndex([], name="Date"),
        )

    monkeypatch.setattr(yahoo_client.yf, "download", download)

    results = await asyncio.gather(
        yahoo_client._fetch_ohlcv_raw("AAPL", days=1, empty_ok=True),
        yahoo_client._fetch_ohlcv_raw("MSFT", days=1, empty_ok=True),
    )

    assert max_active == 1
    assert all(frame.empty for frame in results)
