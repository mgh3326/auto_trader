import logging
from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import yahoo_ohlcv_cache
from tests.ohlcv_cache_fakes import FakeRedis


def _build_daily_frame(end_date: date, rows: int) -> pd.DataFrame:
    dates = [end_date - timedelta(days=index) for index in range(rows)]
    dates.sort()
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0 + idx for idx in range(rows)],
            "high": [105.0 + idx for idx in range(rows)],
            "low": [95.0 + idx for idx in range(rows)],
            "close": [102.0 + idx for idx in range(rows)],
            "volume": [1000.0 + idx for idx in range(rows)],
            "value": [10000.0 + idx for idx in range(rows)],
        }
    )


@pytest.fixture(autouse=True)
def _reset_cache_state():
    if hasattr(yahoo_ohlcv_cache, "_REDIS_CLIENT"):
        yahoo_ohlcv_cache._REDIS_CLIENT = None
    if hasattr(yahoo_ohlcv_cache, "_FALLBACK_COUNT"):
        yahoo_ohlcv_cache._FALLBACK_COUNT = 0
    yield
    if hasattr(yahoo_ohlcv_cache, "_REDIS_CLIENT"):
        yahoo_ohlcv_cache._REDIS_CLIENT = None
    if hasattr(yahoo_ohlcv_cache, "_FALLBACK_COUNT"):
        yahoo_ohlcv_cache._FALLBACK_COUNT = 0


@pytest.mark.asyncio
async def test_get_closed_candles_cache_hit_returns_without_raw_fetch(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    ticker = "AAPL"
    dates_key, rows_key, _, _ = yahoo_ohlcv_cache._keys(ticker, "day")

    await yahoo_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 3),
    )

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        400,
        raising=False,
    )

    fetch_mock = AsyncMock()

    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=3,
        period="day",
        raw_fetcher=fetch_mock,
    )

    assert result is not None
    assert len(result) == 3
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_candles_month_accepts_same_bucket_different_label(
    monkeypatch,
):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 1, 2)
    cached_month_label_date = date(2026, 1, 1)
    ticker = "AAPL"
    dates_key, rows_key, _, _ = yahoo_ohlcv_cache._keys(ticker, "month")

    await yahoo_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(cached_month_label_date, 1),
    )

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        400,
        raising=False,
    )

    fetch_mock = AsyncMock(return_value=pd.DataFrame(columns=["date"]))

    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=1,
        period="month",
        raw_fetcher=fetch_mock,
    )

    assert result is not None
    assert len(result) == 1
    assert result["date"].iloc[-1] == cached_month_label_date
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_candles_week_accepts_same_bucket_different_label(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 1, 20)
    cached_week_label_date = date(2026, 1, 19)
    ticker = "AAPL"
    dates_key, rows_key, _, _ = yahoo_ohlcv_cache._keys(ticker, "week")

    await yahoo_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(cached_week_label_date, 1),
    )

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        400,
        raising=False,
    )

    fetch_mock = AsyncMock(return_value=pd.DataFrame(columns=["date"]))

    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=1,
        period="week",
        raw_fetcher=fetch_mock,
    )

    assert result is not None
    assert len(result) == 1
    assert result["date"].iloc[-1] == cached_week_label_date
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_candles_month_second_call_is_real_cache_hit(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 1, 2)

    async def month_fetcher(
        ticker: str,
        days: int,
        period: str,
        end_date: datetime | None,
    ) -> pd.DataFrame:
        assert ticker == "AAPL"
        assert period == "month"
        assert end_date is not None
        return _build_daily_frame(date(2026, 1, 1), 1)

    fetch_mock = AsyncMock(side_effect=month_fetcher)

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        400,
        raising=False,
    )

    first = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=1,
        period="month",
        raw_fetcher=fetch_mock,
    )
    second = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=1,
        period="month",
        raw_fetcher=fetch_mock,
    )

    assert first is not None
    assert second is not None
    assert first["date"].iloc[-1] == date(2026, 1, 1)
    assert second["date"].iloc[-1] == date(2026, 1, 1)
    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_get_closed_candles_partial_hit_backfills_only_missing(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    ticker = "AAPL"
    dates_key, rows_key, _, _ = yahoo_ohlcv_cache._keys(ticker, "day")

    await yahoo_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 200),
    )

    async def mock_fetch_ohlcv(
        ticker: str,
        days: int,
        period: str,
        end_date: datetime | None,
    ) -> pd.DataFrame:
        assert ticker == "AAPL"
        assert days == 50
        assert period == "day"
        assert end_date is not None
        assert end_date.date() == target_closed_date - timedelta(days=200)
        return _build_daily_frame(target_closed_date - timedelta(days=200), 50)

    fetch_mock = AsyncMock(side_effect=mock_fetch_ohlcv)

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        400,
        raising=False,
    )

    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=250,
        period="day",
        raw_fetcher=fetch_mock,
    )

    assert result is not None
    assert len(result) == 250
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_closed_candles_trims_excess_rows(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    ticker = "AAPL"
    dates_key, rows_key, _, _ = yahoo_ohlcv_cache._keys(ticker, "day")

    await yahoo_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 8),
    )

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        5,
        raising=False,
    )

    fetch_mock = AsyncMock()
    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=5,
        period="day",
        raw_fetcher=fetch_mock,
    )

    assert result is not None
    assert len(result) == 5
    assert await fake_redis.zcard(dates_key) == 5
    assert len(fake_redis.hashes[rows_key]) == 5
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_candles_oldest_confirmed_returns_latest_without_extra_backfill(
    monkeypatch,
):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    ticker = "AAPL"
    dates_key, rows_key, meta_key, _ = yahoo_ohlcv_cache._keys(ticker, "day")

    await yahoo_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 20),
    )
    await fake_redis.hset(meta_key, {"oldest_confirmed": "true"})

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_max_days",
        400,
        raising=False,
    )

    fetch_mock = AsyncMock()

    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=100,
        period="day",
        raw_fetcher=fetch_mock,
    )

    assert result is not None
    assert len(result) == 20
    assert result["date"].iloc[-1] == target_closed_date
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_candles_returns_none_when_lock_contention_and_cache_stale(
    monkeypatch,
):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    acquire_mock = AsyncMock(return_value=None)
    fetch_mock = AsyncMock()

    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(return_value=fake_redis),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "get_last_closed_bucket_nyse",
        lambda period, now=None: target_closed_date,
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(yahoo_ohlcv_cache, "_acquire_lock", acquire_mock)
    monkeypatch.setattr(yahoo_ohlcv_cache.asyncio, "sleep", AsyncMock())

    result = await yahoo_ohlcv_cache.get_closed_candles(
        "AAPL",
        count=30,
        period="day",
        raw_fetcher=fetch_mock,
    )

    assert result is None
    assert acquire_mock.await_count == 3
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_candles_returns_none_on_redis_failure_with_fallback_log(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(
        yahoo_ohlcv_cache,
        "_get_redis_client",
        AsyncMock(side_effect=RuntimeError("redis unavailable")),
    )
    monkeypatch.setattr(
        yahoo_ohlcv_cache.settings,
        "yahoo_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    with caplog.at_level(logging.WARNING):
        result = await yahoo_ohlcv_cache.get_closed_candles(
            "AAPL",
            count=30,
            period="day",
            raw_fetcher=AsyncMock(),
        )

    assert result is None
    assert yahoo_ohlcv_cache._FALLBACK_COUNT == 1
    assert any("yahoo_ohlcv_cache fallback" in message for message in caplog.messages)
