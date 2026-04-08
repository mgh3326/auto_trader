from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import upbit_ohlcv_cache
from tests.ohlcv_cache_fakes import FakeRedis


def _build_daily_frame(end_date: date, rows: int) -> pd.DataFrame:
    dates = [end_date - timedelta(days=index) for index in range(rows)]
    dates.sort()
    return pd.DataFrame(
        {
            "date": dates,
            "open": [10000.0 + idx for idx in range(rows)],
            "high": [10100.0 + idx for idx in range(rows)],
            "low": [9900.0 + idx for idx in range(rows)],
            "close": [10050.0 + idx for idx in range(rows)],
            "volume": [1000.0 + idx for idx in range(rows)],
            "value": [10000000.0 + idx for idx in range(rows)],
        }
    )


def test_get_last_closed_bucket_kst_day_before_anchor():
    now = datetime(2026, 2, 17, 8, 59, 59, tzinfo=ZoneInfo("Asia/Seoul"))
    assert upbit_ohlcv_cache.get_last_closed_bucket_kst("day", now) == date(2026, 2, 15)


def test_get_last_closed_bucket_kst_day_after_anchor():
    now = datetime(2026, 2, 17, 9, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert upbit_ohlcv_cache.get_last_closed_bucket_kst("day", now) == date(2026, 2, 16)


def test_get_last_closed_bucket_kst_week_before_anchor():
    now = datetime(2026, 2, 16, 8, 30, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert upbit_ohlcv_cache.get_last_closed_bucket_kst("week", now) == date(2026, 2, 2)


def test_get_last_closed_bucket_kst_week_after_anchor():
    now = datetime(2026, 2, 16, 9, 1, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert upbit_ohlcv_cache.get_last_closed_bucket_kst("week", now) == date(2026, 2, 9)


def test_get_last_closed_bucket_kst_month_before_anchor():
    now = datetime(2026, 2, 1, 8, 59, 59, tzinfo=ZoneInfo("Asia/Seoul"))
    assert upbit_ohlcv_cache.get_last_closed_bucket_kst("month", now) == date(
        2025, 12, 1
    )


def test_get_last_closed_bucket_kst_month_after_anchor():
    now = datetime(2026, 2, 1, 9, 0, 0, tzinfo=ZoneInfo("Asia/Seoul"))
    assert upbit_ohlcv_cache.get_last_closed_bucket_kst("month", now) == date(
        2026, 1, 1
    )


def test_bucket_gap_count_week_uses_bucket_distance():
    assert (
        upbit_ohlcv_cache._bucket_gap_count("week", date(2026, 2, 9), date(2026, 2, 16))
        == 1
    )


def test_bucket_gap_count_month_uses_bucket_distance():
    assert (
        upbit_ohlcv_cache._bucket_gap_count("month", date(2026, 1, 1), date(2026, 2, 1))
        == 1
    )


def test_keys_include_period_dimension():
    day_keys = upbit_ohlcv_cache._keys("KRW-BTC", "day")
    week_keys = upbit_ohlcv_cache._keys("KRW-BTC", "week")

    assert day_keys[0] != week_keys[0]
    assert "upbit:ohlcv:day:v1:KRW-BTC" in day_keys[0]
    assert "upbit:ohlcv:week:v1:KRW-BTC" in week_keys[0]


@pytest.mark.asyncio
async def test_get_closed_candles_supports_week(monkeypatch):
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", False, raising=False
    )

    result = await upbit_ohlcv_cache.get_closed_candles(
        "KRW-BTC", count=5, period="week"
    )
    assert isinstance(result, pd.DataFrame) or result is None


@pytest.fixture(autouse=True)
def _reset_cache_state():
    upbit_ohlcv_cache._REDIS_CLIENT = None
    upbit_ohlcv_cache._FALLBACK_COUNT = 0
    yield
    upbit_ohlcv_cache._REDIS_CLIENT = None
    upbit_ohlcv_cache._FALLBACK_COUNT = 0


@pytest.mark.asyncio
async def test_get_closed_daily_candles_cache_hit(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    market = "KRW-BTC"
    dates_key, rows_key, _, _ = upbit_ohlcv_cache._keys(market)

    await upbit_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 3),
    )

    async def mock_get_redis_client():
        return fake_redis

    fetch_mock = AsyncMock()

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: target_closed_date,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=3)

    assert result is not None
    assert len(result) == 3
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_partial_hit_backfills_only_missing(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    market = "KRW-BTC"
    dates_key, rows_key, _, _ = upbit_ohlcv_cache._keys(market)

    await upbit_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 200),
    )

    async def mock_get_redis_client():
        return fake_redis

    async def mock_fetch_ohlcv(market: str, days: int, period: str, end_date: datetime):
        assert market == "KRW-BTC"
        assert days == 50
        assert period == "day"
        assert end_date.date() == target_closed_date - timedelta(days=200)
        return _build_daily_frame(target_closed_date - timedelta(days=200), 50)

    fetch_mock = AsyncMock(side_effect=mock_fetch_ohlcv)

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: target_closed_date,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=250)

    assert result is not None
    assert len(result) == 250
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_marks_oldest_confirmed_for_new_listing(
    monkeypatch,
):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    market = "KRW-NEW"
    _, _, meta_key, _ = upbit_ohlcv_cache._keys(market)

    async def mock_get_redis_client():
        return fake_redis

    fetch_mock = AsyncMock(return_value=_build_daily_frame(target_closed_date, 20))

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: target_closed_date,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    first = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=100)
    second = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=100)

    assert first is not None
    assert second is not None
    assert len(first) == 20
    assert len(second) == 20
    assert fake_redis.hashes[meta_key]["oldest_confirmed"] == "true"
    # First call may perform one extra backward check before confirming oldest.
    assert fetch_mock.await_count == 2


@pytest.mark.asyncio
async def test_get_closed_daily_candles_trims_excess_rows(monkeypatch):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    market = "KRW-TRIM"
    dates_key, rows_key, _, _ = upbit_ohlcv_cache._keys(market)

    await upbit_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(target_closed_date, 8),
    )

    async def mock_get_redis_client():
        return fake_redis

    fetch_mock = AsyncMock()

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: target_closed_date,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_max_days", 5, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=5)

    assert result is not None
    assert len(result) == 5
    assert await fake_redis.zcard(dates_key) == 5
    assert len(fake_redis.hashes[rows_key]) == 5
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_refreshes_latest_closed_even_when_count_satisfied(
    monkeypatch,
):
    fake_redis = FakeRedis()
    previous_target = date(2026, 2, 14)
    next_target = previous_target + timedelta(days=1)
    market = "KRW-BTC"
    dates_key, rows_key, _, _ = upbit_ohlcv_cache._keys(market)

    await upbit_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(previous_target, 250),
    )

    async def mock_get_redis_client():
        return fake_redis

    async def mock_fetch_ohlcv(market: str, days: int, period: str, end_date: datetime):
        assert market == "KRW-BTC"
        assert days == 1
        assert period == "day"
        assert end_date.date() == next_target
        return _build_daily_frame(next_target, 1)

    fetch_mock = AsyncMock(side_effect=mock_fetch_ohlcv)

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: next_target,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=200)

    assert result is not None
    assert len(result) == 200
    assert result["date"].iloc[-1] == next_target
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_oldest_confirmed_still_refreshes_latest(
    monkeypatch,
):
    fake_redis = FakeRedis()
    previous_target = date(2026, 2, 14)
    next_target = previous_target + timedelta(days=1)
    market = "KRW-NEW"
    dates_key, rows_key, meta_key, _ = upbit_ohlcv_cache._keys(market)

    await upbit_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(previous_target, 20),
    )
    await fake_redis.hset(meta_key, {"oldest_confirmed": "true"})

    async def mock_get_redis_client():
        return fake_redis

    async def mock_fetch_ohlcv(market: str, days: int, period: str, end_date: datetime):
        assert market == "KRW-NEW"
        assert days == 1
        assert period == "day"
        assert end_date.date() == next_target
        return _build_daily_frame(next_target, 1)

    fetch_mock = AsyncMock(side_effect=mock_fetch_ohlcv)

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: next_target,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=100)

    assert result is not None
    assert len(result) == 21
    assert result["date"].iloc[-1] == next_target
    fetch_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_returns_none_when_lock_contention_and_cache_insufficient(
    monkeypatch,
):
    fake_redis = FakeRedis()
    target_closed_date = date(2026, 2, 14)
    market = "KRW-LOCK-MISS"
    acquire_mock = AsyncMock(return_value=None)
    sleep_mock = AsyncMock()
    fetch_mock = AsyncMock()

    async def mock_get_redis_client():
        return fake_redis

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: target_closed_date,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache, "_acquire_lock", acquire_mock)
    monkeypatch.setattr(upbit_ohlcv_cache.asyncio, "sleep", sleep_mock)
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=30)

    assert result is None
    assert acquire_mock.await_count == 3
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_returns_cached_when_lock_contention_but_data_becomes_sufficient(
    monkeypatch,
):
    fake_redis = FakeRedis()
    previous_target = date(2026, 2, 14)
    next_target = previous_target + timedelta(days=1)
    market = "KRW-LOCK-CACHED"
    dates_key, rows_key, _, _ = upbit_ohlcv_cache._keys(market)

    await upbit_ohlcv_cache._upsert_rows(
        fake_redis,
        dates_key,
        rows_key,
        _build_daily_frame(previous_target, 2),
    )

    async def mock_get_redis_client():
        return fake_redis

    acquire_calls = {"count": 0}

    async def mock_acquire_lock(redis_client, lock_key, ttl_seconds):
        del lock_key, ttl_seconds
        acquire_calls["count"] += 1
        if acquire_calls["count"] == 1:
            await upbit_ohlcv_cache._upsert_rows(
                redis_client,
                dates_key,
                rows_key,
                _build_daily_frame(next_target, 1),
            )
        return None

    sleep_mock = AsyncMock()
    fetch_mock = AsyncMock()

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache,
        "get_target_closed_date_kst",
        lambda now=None: next_target,
    )
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(upbit_ohlcv_cache, "_acquire_lock", mock_acquire_lock)
    monkeypatch.setattr(upbit_ohlcv_cache.asyncio, "sleep", sleep_mock)
    monkeypatch.setattr(upbit_ohlcv_cache.upbit_service, "fetch_ohlcv", fetch_mock)

    result = await upbit_ohlcv_cache.get_closed_daily_candles(market, count=2)

    assert result is not None
    assert len(result) == 2
    assert result["date"].iloc[-1] == next_target
    assert acquire_calls["count"] == 3
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_returns_none_on_redis_failure(monkeypatch):
    async def mock_get_redis_client():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(upbit_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        upbit_ohlcv_cache.settings, "upbit_ohlcv_cache_enabled", True, raising=False
    )

    result = await upbit_ohlcv_cache.get_closed_daily_candles("KRW-BTC", count=30)

    assert result is None
    assert upbit_ohlcv_cache._FALLBACK_COUNT == 1
