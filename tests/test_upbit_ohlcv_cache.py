from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.services import upbit_ohlcv_cache


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
            "open": [10000.0 + idx for idx in range(rows)],
            "high": [10100.0 + idx for idx in range(rows)],
            "low": [9900.0 + idx for idx in range(rows)],
            "close": [10050.0 + idx for idx in range(rows)],
            "volume": [1000.0 + idx for idx in range(rows)],
            "value": [10000000.0 + idx for idx in range(rows)],
        }
    )


@pytest.fixture(autouse=True)
def _reset_cache_state():
    upbit_ohlcv_cache._REDIS_CLIENT = None
    upbit_ohlcv_cache._FALLBACK_COUNT = 0
    yield
    upbit_ohlcv_cache._REDIS_CLIENT = None
    upbit_ohlcv_cache._FALLBACK_COUNT = 0


@pytest.mark.asyncio
async def test_get_closed_daily_candles_cache_hit(monkeypatch):
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
    fake_redis = _FakeRedis()
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
