import logging
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import yahoo_ohlcv_cache


def _build_fake_calendar(close_map: dict[date, datetime]):
    index = pd.DatetimeIndex([pd.Timestamp(session_date) for session_date in close_map])
    close_values = [pd.Timestamp(close_map[session_date]) for session_date in close_map]
    schedule = pd.DataFrame({"close": close_values}, index=index)

    class _FakeCalendar:
        def __init__(self, schedule_frame: pd.DataFrame):
            self.schedule = schedule_frame

    return _FakeCalendar(schedule)


def test_get_last_closed_bucket_nyse_day_uses_last_session_close(monkeypatch):
    fake_calendar = _build_fake_calendar(
        {
            date(2026, 2, 14): datetime(2026, 2, 14, 21, 0, tzinfo=UTC),
            date(2026, 2, 16): datetime(2026, 2, 16, 21, 0, tzinfo=UTC),
            date(2026, 2, 17): datetime(2026, 2, 17, 21, 0, tzinfo=UTC),
            date(2026, 2, 18): datetime(2026, 2, 18, 21, 0, tzinfo=UTC),
        }
    )
    monkeypatch.setattr(yahoo_ohlcv_cache, "_get_xnys_calendar", lambda: fake_calendar)

    now = datetime(2026, 2, 17, 21, 30, tzinfo=UTC)

    assert yahoo_ohlcv_cache.get_last_closed_bucket_nyse("day", now) == date(
        2026, 2, 17
    )


def test_get_last_closed_bucket_nyse_week_requires_final_session_close(monkeypatch):
    fake_calendar = _build_fake_calendar(
        {
            date(2026, 2, 9): datetime(2026, 2, 9, 21, 0, tzinfo=UTC),
            date(2026, 2, 13): datetime(2026, 2, 13, 21, 0, tzinfo=UTC),
            date(2026, 2, 16): datetime(2026, 2, 16, 21, 0, tzinfo=UTC),
            date(2026, 2, 20): datetime(2026, 2, 20, 21, 0, tzinfo=UTC),
        }
    )
    monkeypatch.setattr(yahoo_ohlcv_cache, "_get_xnys_calendar", lambda: fake_calendar)

    now = datetime(2026, 2, 18, 20, 30, tzinfo=UTC)

    assert yahoo_ohlcv_cache.get_last_closed_bucket_nyse("week", now) == date(
        2026, 2, 9
    )


def test_get_last_closed_bucket_nyse_month_requires_month_end_session_close(
    monkeypatch,
):
    fake_calendar = _build_fake_calendar(
        {
            date(2026, 1, 2): datetime(2026, 1, 2, 21, 0, tzinfo=UTC),
            date(2026, 1, 30): datetime(2026, 1, 30, 21, 0, tzinfo=UTC),
            date(2026, 2, 2): datetime(2026, 2, 2, 21, 0, tzinfo=UTC),
            date(2026, 2, 27): datetime(2026, 2, 27, 21, 0, tzinfo=UTC),
        }
    )
    monkeypatch.setattr(yahoo_ohlcv_cache, "_get_xnys_calendar", lambda: fake_calendar)

    now = datetime(2026, 2, 20, 21, 30, tzinfo=UTC)

    assert yahoo_ohlcv_cache.get_last_closed_bucket_nyse("month", now) == date(
        2026, 1, 2
    )


def test_get_last_closed_bucket_nyse_invalid_period_raises_value_error():
    with pytest.raises(ValueError):
        yahoo_ohlcv_cache.get_last_closed_bucket_nyse("hour")


class _FakePipeline:
    def __init__(self, redis_client):
        self.redis_client = redis_client
        self.commands: list[tuple[str, tuple, dict]] = []

    def zremrangebyrank(self, *args, **kwargs):
        self.commands.append(("zremrangebyrank", args, kwargs))
        return self

    def hdel(self, *args, **kwargs):
        self.commands.append(("hdel", args, kwargs))
        return self

    def zadd(self, *args, **kwargs):
        self.commands.append(("zadd", args, kwargs))
        return self

    def hset(self, *args, **kwargs):
        self.commands.append(("hset", args, kwargs))
        return self

    async def execute(self):
        results = []
        for method_name, args, kwargs in self.commands:
            method = getattr(self.redis_client, method_name)
            results.append(await method(*args, **kwargs))
        self.commands.clear()
        return results


class _FakeRedis:
    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.hashes: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}

    def pipeline(self, transaction: bool = True):
        return _FakePipeline(self)

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def eval(self, script: str, key_count: int, key: str, token: str):
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0

    async def zadd(self, key: str, mapping: dict[str, int | float]):
        zset = self.zsets.setdefault(key, {})
        inserted = 0
        for member, score in mapping.items():
            if member not in zset:
                inserted += 1
            zset[member] = float(score)
        return inserted

    async def zcard(self, key: str):
        return len(self.zsets.get(key, {}))

    async def zcount(
        self, key: str, minimum: str | int | float, maximum: str | int | float
    ):
        zset = self.zsets.get(key, {})
        min_score = self._normalize_score(minimum, is_min=True)
        max_score = self._normalize_score(maximum, is_min=False)
        return sum(1 for score in zset.values() if min_score <= score <= max_score)

    async def zrange(self, key: str, start: int, end: int):
        items = sorted(
            self.zsets.get(key, {}).items(), key=lambda item: (item[1], item[0])
        )
        members = [member for member, _ in items]
        if not members:
            return []
        if end < 0:
            end = len(members) + end
        if end < start:
            return []
        return members[start : end + 1]

    async def zrevrangebyscore(
        self,
        key: str,
        maximum: str | int | float,
        minimum: str | int | float,
        start: int = 0,
        num: int | None = None,
    ):
        zset = self.zsets.get(key, {})
        min_score = self._normalize_score(minimum, is_min=True)
        max_score = self._normalize_score(maximum, is_min=False)
        items = [
            (member, score)
            for member, score in zset.items()
            if min_score <= score <= max_score
        ]
        items.sort(key=lambda item: (item[1], item[0]), reverse=True)
        members = [member for member, _ in items]
        if num is None:
            return members[start:]
        return members[start : start + num]

    async def zremrangebyrank(self, key: str, start: int, end: int):
        members = await self.zrange(key, 0, -1)
        if not members:
            return 0
        if end < 0:
            end = len(members) + end
        if end < start:
            return 0
        removable = members[start : end + 1]
        zset = self.zsets.get(key, {})
        for member in removable:
            zset.pop(member, None)
        return len(removable)

    async def hset(self, key: str, mapping: dict[str, str]):
        target = self.hashes.setdefault(key, {})
        inserted = 0
        for field, value in mapping.items():
            if field not in target:
                inserted += 1
            target[field] = value
        return inserted

    async def hmget(self, key: str, fields: list[str]):
        target = self.hashes.get(key, {})
        return [target.get(field) for field in fields]

    async def hgetall(self, key: str):
        return dict(self.hashes.get(key, {}))

    async def hdel(self, key: str, *fields: str):
        target = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in target:
                removed += 1
                target.pop(field, None)
        return removed

    @staticmethod
    def _normalize_score(value: str | int | float, is_min: bool) -> float:
        if isinstance(value, str):
            if value == "-inf":
                return float("-inf")
            if value == "+inf":
                return float("inf")
        parsed = float(value)
        if parsed == float("-inf") and not is_min:
            return float("-inf")
        if parsed == float("inf") and is_min:
            return float("inf")
        return parsed


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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
