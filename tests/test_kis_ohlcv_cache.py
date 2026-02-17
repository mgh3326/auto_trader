from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services import kis_ohlcv_cache


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

    def expire(self, *args, **kwargs):
        self.commands.append(("expire", args, kwargs))
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

    async def get(self, key: str):
        return self.strings.get(key)

    async def eval(self, script: str, key_count: int, key: str, token: str):
        if self.strings.get(key) == token:
            self.strings.pop(key, None)
            return 1
        return 0

    async def expire(self, key: str, ttl: int):
        return True

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
        min_score = self._normalize_score(minimum)
        max_score = self._normalize_score(maximum)
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
    def _normalize_score(value: str | int | float) -> float:
        if isinstance(value, str):
            if value == "-inf":
                return float("-inf")
            if value == "+inf":
                return float("inf")
        return float(value)


def _build_daily_frame(end_date: date, rows: int) -> pd.DataFrame:
    dates = [end_date - timedelta(days=index) for index in range(rows)]
    dates.sort()
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0 + idx for idx in range(rows)],
            "high": [101.0 + idx for idx in range(rows)],
            "low": [99.0 + idx for idx in range(rows)],
            "close": [100.5 + idx for idx in range(rows)],
            "volume": [1000.0 + idx for idx in range(rows)],
            "value": [100000.0 + idx for idx in range(rows)],
        }
    )


@pytest.fixture(autouse=True)
def _reset_cache_state():
    kis_ohlcv_cache._REDIS_CLIENT = None
    yield
    kis_ohlcv_cache._REDIS_CLIENT = None


def test_expected_asof_et_handles_est_edt_and_weekend_rollbacks():
    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 1, 15, 20, 30, tzinfo=UTC)
    ) == date(2026, 1, 14)
    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 1, 15, 21, 30, tzinfo=UTC)
    ) == date(2026, 1, 15)

    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 7, 15, 19, 30, tzinfo=UTC)
    ) == date(2026, 7, 14)

    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 7, 18, 18, 0, tzinfo=UTC)
    ) == date(2026, 7, 17)


def test_expected_asof_et_same_for_2330_and_0130_kst():
    kst = ZoneInfo("Asia/Seoul")
    first = datetime(2026, 2, 17, 23, 30, tzinfo=kst).astimezone(UTC)
    second = datetime(2026, 2, 18, 1, 30, tzinfo=kst).astimezone(UTC)

    assert kis_ohlcv_cache.expected_asof_et(first) == date(2026, 2, 16)
    assert kis_ohlcv_cache.expected_asof_et(first) == kis_ohlcv_cache.expected_asof_et(
        second
    )


def test_expected_asof_et_uses_1600_close_boundary_for_est_and_edt():
    # EST: 20:59 UTC = 15:59 ET, 21:00 UTC = 16:00 ET
    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 1, 15, 20, 59, tzinfo=UTC)
    ) == date(2026, 1, 14)
    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 1, 15, 21, 0, tzinfo=UTC)
    ) == date(2026, 1, 15)

    # EDT: 19:59 UTC = 15:59 ET, 20:00 UTC = 16:00 ET
    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 7, 15, 19, 59, tzinfo=UTC)
    ) == date(2026, 7, 14)
    assert kis_ohlcv_cache.expected_asof_et(
        datetime(2026, 7, 15, 20, 0, tzinfo=UTC)
    ) == date(2026, 7, 15)


def test_kis_cache_key_format_and_legacy_key_format():
    dates_key, rows_key, meta_key, lock_key = kis_ohlcv_cache._keys(
        "AAPL", "NASD", "equity_us"
    )
    assert dates_key == "kis:ohlcv:day:v1:equity_us:NASD:AAPL:dates"
    assert rows_key == "kis:ohlcv:day:v1:equity_us:NASD:AAPL:rows"
    assert meta_key == "kis:ohlcv:day:v1:equity_us:NASD:AAPL:meta"
    assert lock_key == "kis:ohlcv:day:v1:equity_us:NASD:AAPL:lock"

    legacy_dates_key, _, _, _ = kis_ohlcv_cache._legacy_keys("AAPL", "NASD", "equity_us")
    assert legacy_dates_key == "kis:ohlcv:equity_us:day:v1:NASD:AAPL:dates"


@pytest.mark.asyncio
async def test_get_closed_daily_candles_miss_fetch_store_then_hit(monkeypatch):
    fake_redis = _FakeRedis()
    target_asof = date(2026, 2, 16)

    async def mock_get_redis_client():
        return fake_redis

    fetch_mock = AsyncMock(return_value=_build_daily_frame(target_asof, 3))

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache, "expected_asof_et", lambda now_utc: target_asof
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_ttl_seconds", 300, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_lock_ttl_seconds",
        10,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_probe_retry_seconds",
        1800,
        raising=False,
    )

    first = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=datetime(2026, 2, 17, 15, 0, tzinfo=UTC),
    )
    second = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=datetime(2026, 2, 17, 15, 30, tzinfo=UTC),
    )

    assert first is not None
    assert second is not None
    assert len(first) == 3
    assert len(second) == 3
    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_get_closed_daily_candles_reads_legacy_cache_for_compat(monkeypatch):
    fake_redis = _FakeRedis()
    target_asof = date(2026, 2, 16)

    async def mock_get_redis_client():
        return fake_redis

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache, "expected_asof_et", lambda now_utc: target_asof
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_ttl_seconds", 300, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_lock_ttl_seconds",
        10,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_probe_retry_seconds",
        1800,
        raising=False,
    )

    legacy_dates_key, legacy_rows_key, _, _ = kis_ohlcv_cache._legacy_keys(
        "AAPL", "NASD", "equity_us"
    )
    await kis_ohlcv_cache._upsert_rows(
        fake_redis,
        legacy_dates_key,
        legacy_rows_key,
        _build_daily_frame(target_asof, 3),
    )

    fetch_mock = AsyncMock()
    result = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=datetime(2026, 2, 17, 15, 0, tzinfo=UTC),
    )

    assert result is not None
    assert len(result) == 3
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_single_fetch_under_lock_contention(monkeypatch):
    fake_redis = _FakeRedis()
    target_asof = date(2026, 2, 16)

    async def mock_get_redis_client():
        return fake_redis

    async def slow_fetch(
        *, symbol: str, exchange_code: str, n: int, end_date: date | None
    ):
        await asyncio.sleep(0.05)
        return _build_daily_frame(target_asof, 3)

    fetch_mock = AsyncMock(side_effect=slow_fetch)

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache, "expected_asof_et", lambda now_utc: target_asof
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_ttl_seconds", 300, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_lock_ttl_seconds",
        10,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_probe_retry_seconds",
        1800,
        raising=False,
    )

    async def _load_once():
        return await kis_ohlcv_cache.get_closed_daily_candles(
            symbol="AAPL",
            exchange_code="NASD",
            count=3,
            instrument_type="equity_us",
            raw_fetcher=fetch_mock,
            now_utc=datetime(2026, 2, 17, 15, 0, tzinfo=UTC),
        )

    first, second = await asyncio.gather(_load_once(), _load_once())
    assert first is not None
    assert second is not None
    assert len(first) == 3
    assert len(second) == 3
    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_get_closed_daily_candles_returns_none_on_redis_error(monkeypatch):
    async def mock_get_redis_client():
        raise RuntimeError("redis unavailable")

    fetch_mock = AsyncMock()

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_enabled", True, raising=False
    )

    result = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=datetime(2026, 2, 17, 15, 0, tzinfo=UTC),
    )

    assert result is None
    fetch_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_closed_daily_candles_sets_probe_suppression_on_partial_latest(
    monkeypatch,
):
    fake_redis = _FakeRedis()
    target_asof = date(2026, 2, 17)
    partial_latest = date(2026, 2, 14)

    async def mock_get_redis_client():
        return fake_redis

    fetch_mock = AsyncMock(return_value=_build_daily_frame(partial_latest, 3))

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache, "expected_asof_et", lambda now_utc: target_asof
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_ttl_seconds", 300, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_lock_ttl_seconds",
        10,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_probe_retry_seconds",
        1800,
        raising=False,
    )

    first = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=datetime(2026, 2, 17, 15, 0, tzinfo=UTC),
    )
    second = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=datetime(2026, 2, 17, 15, 5, tzinfo=UTC),
    )

    assert first is not None
    assert second is not None
    assert len(first) == 3
    assert len(second) == 3
    assert fetch_mock.await_count == 1


@pytest.mark.asyncio
async def test_get_closed_daily_candles_same_asof_hit_for_2330_and_0130_kst(
    monkeypatch,
):
    fake_redis = _FakeRedis()
    target_asof = date(2026, 2, 16)
    kst = ZoneInfo("Asia/Seoul")

    async def mock_get_redis_client():
        return fake_redis

    fetch_mock = AsyncMock(return_value=_build_daily_frame(target_asof, 5))

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_enabled", True, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_ttl_seconds", 300, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings, "kis_ohlcv_cache_max_days", 400, raising=False
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_lock_ttl_seconds",
        10,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_probe_retry_seconds",
        1800,
        raising=False,
    )

    first_now = datetime(2026, 2, 17, 23, 30, tzinfo=kst).astimezone(UTC)
    second_now = datetime(2026, 2, 18, 1, 30, tzinfo=kst).astimezone(UTC)

    first = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=first_now,
    )
    second = await kis_ohlcv_cache.get_closed_daily_candles(
        symbol="AAPL",
        exchange_code="NASD",
        count=3,
        instrument_type="equity_us",
        raw_fetcher=fetch_mock,
        now_utc=second_now,
    )

    assert first is not None
    assert second is not None
    assert len(first) == 3
    assert len(second) == 3
    assert fetch_mock.await_count == 1
