from datetime import date
from unittest.mock import ANY, AsyncMock

import pandas as pd
import pytest

from app.services import yahoo
from app.services import yahoo_ohlcv_cache as yahoo_cache


@pytest.mark.asyncio
async def test_fetch_ohlcv_uses_cache_for_day(monkeypatch):
    cached = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 14),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 2, 15),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 1100,
            },
            {
                "date": date(2026, 2, 16),
                "open": 102,
                "high": 103,
                "low": 101,
                "close": 102,
                "volume": 1200,
            },
        ]
    )

    cache_mock = AsyncMock(return_value=cached)
    monkeypatch.setattr(yahoo_cache, "get_closed_candles", cache_mock)
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=3, period="day")

    assert len(result) == 3
    cache_mock.assert_awaited_once_with(
        "AAPL",
        count=3,
        period="day",
        raw_fetcher=ANY,
    )


@pytest.mark.asyncio
async def test_fetch_ohlcv_filters_unclosed_bucket_on_cache_none(monkeypatch):
    monkeypatch.setattr(
        yahoo_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        yahoo_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: date(2026, 2, 14),
    )

    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 14),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 2, 15),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 1100,
            },
        ]
    )

    monkeypatch.setattr(yahoo, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=2, period="day")

    assert result["date"].max() == date(2026, 2, 14)


@pytest.mark.asyncio
async def test_fetch_ohlcv_week_excludes_current_open_week(monkeypatch):
    monkeypatch.setattr(
        yahoo_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        yahoo_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: date(2026, 2, 9),
    )

    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 9),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 2, 16),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 1100,
            },
        ]
    )

    monkeypatch.setattr(yahoo, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=2, period="week")

    assert len(result) == 1
    assert result["date"].iloc[-1] == date(2026, 2, 9)


@pytest.mark.asyncio
async def test_fetch_ohlcv_week_keeps_closed_week_with_different_label(monkeypatch):
    monkeypatch.setattr(
        yahoo_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        yahoo_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: date(2026, 2, 9),
    )

    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 10),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 2, 17),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 1100,
            },
        ]
    )

    monkeypatch.setattr(yahoo, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=2, period="week")

    assert len(result) == 1
    assert result["date"].iloc[-1] == date(2026, 2, 10)


@pytest.mark.asyncio
async def test_fetch_ohlcv_month_excludes_current_open_month(monkeypatch):
    monkeypatch.setattr(
        yahoo_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        yahoo_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: date(2026, 1, 1),
    )

    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 1, 1),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 2, 1),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 1100,
            },
        ]
    )

    monkeypatch.setattr(yahoo, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=2, period="month")

    assert len(result) == 1
    assert result["date"].iloc[-1] == date(2026, 1, 1)


@pytest.mark.asyncio
async def test_fetch_ohlcv_month_keeps_closed_month_with_different_label(monkeypatch):
    monkeypatch.setattr(
        yahoo_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        yahoo_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: date(2026, 1, 1),
    )

    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 1, 2),
                "open": 100,
                "high": 101,
                "low": 99,
                "close": 100,
                "volume": 1000,
            },
            {
                "date": date(2026, 2, 2),
                "open": 101,
                "high": 102,
                "low": 100,
                "close": 101,
                "volume": 1100,
            },
        ]
    )

    monkeypatch.setattr(yahoo, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        yahoo.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await yahoo.fetch_ohlcv("AAPL", days=2, period="month")

    assert len(result) == 1
    assert result["date"].iloc[-1] == date(2026, 1, 2)
