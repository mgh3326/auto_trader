from datetime import date
from unittest.mock import ANY, AsyncMock

import pandas as pd
import pytest

from app.integrations import upbit
from app.services import upbit_ohlcv_cache as upbit_cache


@pytest.mark.asyncio
async def test_fetch_ohlcv_uses_cache_for_day(monkeypatch):
    cached = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 14),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "value": 1,
            }
        ]
    )
    cache_mock = AsyncMock(return_value=cached)

    monkeypatch.setattr(upbit_cache, "get_closed_candles", cache_mock)
    monkeypatch.setattr(
        upbit.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(upbit, "_request_json", AsyncMock(return_value=[]))

    result = await upbit.fetch_ohlcv("KRW-BTC", days=1, period="day")

    assert len(result) == 1
    cache_mock.assert_awaited_once_with(
        "KRW-BTC",
        count=1,
        period="day",
        raw_fetcher=ANY,
    )


@pytest.mark.asyncio
async def test_fetch_ohlcv_filters_unclosed_bucket_on_cache_none(monkeypatch):
    monkeypatch.setattr(
        upbit_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 16),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "value": 1,
            },
            {
                "date": date(2026, 2, 17),
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 2,
                "value": 2,
            },
        ]
    )
    monkeypatch.setattr(upbit, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        upbit_cache,
        "get_last_closed_bucket_kst",
        lambda period, now=None: date(2026, 2, 16),
    )
    monkeypatch.setattr(
        upbit.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )

    result = await upbit.fetch_ohlcv("KRW-BTC", days=2, period="day")

    assert result["date"].max() == date(2026, 2, 16)


@pytest.mark.asyncio
async def test_fetch_ohlcv_week_excludes_current_open_week(monkeypatch):
    monkeypatch.setattr(
        upbit_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 2, 9),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "value": 1,
            },
            {
                "date": date(2026, 2, 16),
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 2,
                "value": 2,
            },
        ]
    )
    monkeypatch.setattr(upbit, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        upbit_cache,
        "get_last_closed_bucket_kst",
        lambda period, now=None: date(2026, 2, 9),
    )
    monkeypatch.setattr(
        upbit.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )

    result = await upbit.fetch_ohlcv("KRW-BTC", days=2, period="week")

    assert len(result) == 1
    assert result["date"].iloc[-1] == date(2026, 2, 9)


@pytest.mark.asyncio
async def test_fetch_ohlcv_month_excludes_current_open_month(monkeypatch):
    monkeypatch.setattr(
        upbit_cache,
        "get_closed_candles",
        AsyncMock(return_value=None),
    )
    raw = pd.DataFrame(
        [
            {
                "date": date(2026, 1, 1),
                "open": 1,
                "high": 1,
                "low": 1,
                "close": 1,
                "volume": 1,
                "value": 1,
            },
            {
                "date": date(2026, 2, 1),
                "open": 2,
                "high": 2,
                "low": 2,
                "close": 2,
                "volume": 2,
                "value": 2,
            },
        ]
    )
    monkeypatch.setattr(upbit, "_fetch_ohlcv_raw", AsyncMock(return_value=raw))
    monkeypatch.setattr(
        upbit_cache,
        "get_last_closed_bucket_kst",
        lambda period, now=None: date(2026, 1, 1),
    )
    monkeypatch.setattr(
        upbit.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )

    result = await upbit.fetch_ohlcv("KRW-BTC", days=2, period="month")

    assert len(result) == 1
    assert result["date"].iloc[-1] == date(2026, 1, 1)
