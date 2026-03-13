from datetime import date, datetime, timedelta
from typing import Any, cast
from unittest.mock import AsyncMock

import pandas as pd
import pytest

from app.core.timezone import KST
from app.services import kis_ohlcv_cache


class _FakePipeline:
    def __init__(self, redis_client):
        self.redis_client = redis_client
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

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
        del transaction
        return _FakePipeline(self)

    async def set(self, key: str, value: str, nx: bool = False, ex: int | None = None):
        del ex
        if nx and key in self.strings:
            return False
        self.strings[key] = value
        return True

    async def eval(self, script: str, key_count: int, key: str, token: str):
        del script, key_count
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

    async def zrevrange(self, key: str, start: int, end: int):
        items = sorted(
            self.zsets.get(key, {}).items(),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        members = [member for member, _ in items]
        if not members:
            return []
        if end < 0:
            end = len(members) + end
        if end < start:
            return []
        return members[start : end + 1]

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

    async def hdel(self, key: str, *fields: str):
        target = self.hashes.get(key, {})
        removed = 0
        for field in fields:
            if field in target:
                removed += 1
                target.pop(field, None)
        return removed


def _build_daily_frame(rows: int = 2) -> pd.DataFrame:
    base_date = datetime.now().date()
    dates = [base_date - timedelta(days=idx) for idx in range(rows)]
    dates.sort()
    return _build_daily_frame_for_dates(dates)


def _build_daily_frame_for_dates(dates: list[date]) -> pd.DataFrame:
    rows = len(dates)
    return pd.DataFrame(
        {
            "date": dates,
            "open": [100.0 + i for i in range(rows)],
            "high": [101.0 + i for i in range(rows)],
            "low": [99.0 + i for i in range(rows)],
            "close": [100.5 + i for i in range(rows)],
            "volume": [1000 + i for i in range(rows)],
            "value": [100000 + i for i in range(rows)],
        }
    )


def _kst_datetime(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=KST)


@pytest.fixture(autouse=True)
def _reset_cache_state():
    kis_ohlcv_cache._REDIS_CLIENT = None
    kis_ohlcv_cache._FALLBACK_COUNT = 0


@pytest.mark.parametrize(
    ("latest_date", "now", "expected"),
    [
        (datetime(2026, 3, 13).date(), _kst_datetime(2026, 3, 13, 15, 0), False),
        (datetime(2026, 3, 13).date(), _kst_datetime(2026, 3, 13, 8, 59), True),
        (datetime(2026, 3, 13).date(), _kst_datetime(2026, 3, 13, 15, 35), True),
        (datetime(2026, 3, 12).date(), _kst_datetime(2026, 3, 13, 15, 0), False),
        (datetime(2026, 3, 12).date(), _kst_datetime(2026, 3, 13, 8, 59), True),
        (datetime(2026, 3, 11).date(), _kst_datetime(2026, 3, 13, 8, 59), False),
        (datetime(2026, 3, 12).date(), _kst_datetime(2026, 3, 13, 15, 35), False),
        (datetime(2026, 3, 13).date(), _kst_datetime(2026, 3, 14, 12, 0), True),
        (datetime(2026, 3, 12).date(), _kst_datetime(2026, 3, 14, 12, 0), False),
        (datetime(2026, 3, 14).date(), _kst_datetime(2026, 3, 13, 15, 0), True),
    ],
)
def test_is_cache_fresh_for_day_respects_xkrx_session_policy(
    latest_date, now: datetime, expected: bool
):
    frame = _build_daily_frame_for_dates([latest_date])

    assert kis_ohlcv_cache._is_cache_fresh("day", frame, now=now) is expected


def test_is_session_day_kst_uses_xkrx_calendar() -> None:
    assert kis_ohlcv_cache._is_session_day_kst(date(2026, 1, 1)) is False
    assert kis_ohlcv_cache._is_session_day_kst(date(2026, 1, 2)) is True


def _configure_cache_runtime(
    monkeypatch, fake_redis: _FakeRedis, now: datetime
) -> None:
    async def mock_get_redis_client():
        return fake_redis

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(kis_ohlcv_cache, "now_kst", lambda: now)
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_max_days",
        400,
        raising=False,
    )
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_lock_ttl_seconds",
        10,
        raising=False,
    )


def test_keys_include_route_segment_for_same_symbol_period():
    default_keys = kis_ohlcv_cache._keys("005930", "1h")
    routed_keys = kis_ohlcv_cache._keys("005930", "1h", "j")

    assert default_keys != routed_keys
    assert ":J:" in routed_keys[0]


@pytest.mark.asyncio
async def test_get_candles_returns_cached_when_sufficient(monkeypatch):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 15, 35)
    cached = _build_daily_frame_for_dates(
        [datetime(2026, 3, 12).date(), datetime(2026, 3, 13).date()]
    )

    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        cached,
        "day",
    )

    raw_fetcher = AsyncMock(return_value=_build_daily_frame_for_dates([now.date()]))
    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=2,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    assert len(result) == 2
    raw_fetcher.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_candles_refreshes_when_cached_rows_are_stale(monkeypatch):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 15, 0)

    stale = _build_daily_frame_for_dates(
        [datetime(2026, 3, 12).date(), datetime(2026, 3, 13).date()]
    ).assign(close=[100.5, 101.5])
    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        stale,
        "day",
    )

    fresh = _build_daily_frame_for_dates(
        [datetime(2026, 3, 12).date(), datetime(2026, 3, 13).date()]
    ).assign(close=[200.5, 201.5])
    raw_fetcher = AsyncMock(return_value=fresh)

    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=2,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    raw_fetcher.assert_awaited_once_with(2)
    assert len(result) == 2
    assert result["date"].max() == fresh["date"].max()
    assert result.iloc[-1]["close"] == 201.5


@pytest.mark.asyncio
async def test_get_candles_refreshes_intraday_when_only_yesterday_is_cached(
    monkeypatch,
):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 15, 0)

    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        _build_daily_frame_for_dates([datetime(2026, 3, 12).date()]),
        "day",
    )

    fresh = _build_daily_frame_for_dates(
        [datetime(2026, 3, 12).date(), datetime(2026, 3, 13).date()]
    )
    raw_fetcher = AsyncMock(return_value=fresh)

    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=1,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    raw_fetcher.assert_awaited_once_with(1)
    assert result.iloc[-1]["date"] == datetime(2026, 3, 13).date()


@pytest.mark.asyncio
async def test_get_candles_reuses_yesterday_before_open(monkeypatch):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 8, 59)

    cached = _build_daily_frame_for_dates([datetime(2026, 3, 12).date()])
    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        cached,
        "day",
    )

    raw_fetcher = AsyncMock(
        return_value=_build_daily_frame_for_dates([datetime(2026, 3, 13).date()])
    )

    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=1,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    raw_fetcher.assert_not_awaited()
    assert result.iloc[-1]["date"] == datetime(2026, 3, 12).date()


@pytest.mark.asyncio
async def test_get_candles_refreshes_before_open_when_previous_session_missing(
    monkeypatch,
):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 8, 59)

    cached = _build_daily_frame_for_dates([datetime(2026, 3, 11).date()])
    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        cached,
        "day",
    )

    fresh = _build_daily_frame_for_dates([datetime(2026, 3, 12).date()])
    raw_fetcher = AsyncMock(return_value=fresh)

    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=1,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    raw_fetcher.assert_awaited_once_with(1)
    assert result.iloc[-1]["date"] == datetime(2026, 3, 12).date()


@pytest.mark.asyncio
async def test_get_candles_reuses_today_after_cutoff(monkeypatch):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 15, 35)

    cached = _build_daily_frame_for_dates([datetime(2026, 3, 13).date()])
    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        cached,
        "day",
    )

    raw_fetcher = AsyncMock(
        return_value=_build_daily_frame_for_dates([datetime(2026, 3, 14).date()])
    )

    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=1,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    raw_fetcher.assert_not_awaited()
    assert result.iloc[-1]["date"] == datetime(2026, 3, 13).date()


@pytest.mark.asyncio
async def test_get_candles_refreshes_after_cutoff_when_today_row_missing(monkeypatch):
    fake_redis = _FakeRedis()
    symbol = "005930"
    dates_key, rows_key, _, _ = kis_ohlcv_cache._keys(symbol, "day")
    now = _kst_datetime(2026, 3, 13, 15, 35)

    cached = _build_daily_frame_for_dates([datetime(2026, 3, 12).date()])
    await kis_ohlcv_cache._upsert_rows(
        cast(Any, fake_redis),
        dates_key,
        rows_key,
        cached,
        "day",
    )

    fresh = _build_daily_frame_for_dates([datetime(2026, 3, 13).date()])
    raw_fetcher = AsyncMock(return_value=fresh)

    _configure_cache_runtime(monkeypatch, fake_redis, now)

    result = await kis_ohlcv_cache.get_candles(
        symbol=symbol,
        count=1,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    raw_fetcher.assert_awaited_once_with(1)
    assert result.iloc[-1]["date"] == datetime(2026, 3, 13).date()


@pytest.mark.asyncio
async def test_get_candles_fallbacks_to_raw_on_redis_error(monkeypatch):
    async def mock_get_redis_client():
        raise RuntimeError("redis unavailable")

    raw = _build_daily_frame(2)
    raw_fetcher = AsyncMock(return_value=raw)

    monkeypatch.setattr(kis_ohlcv_cache, "_get_redis_client", mock_get_redis_client)
    monkeypatch.setattr(
        kis_ohlcv_cache.settings,
        "kis_ohlcv_cache_enabled",
        True,
        raising=False,
    )

    result = await kis_ohlcv_cache.get_candles(
        symbol="005930",
        count=2,
        period="day",
        raw_fetcher=raw_fetcher,
    )

    assert len(result) == 2
    raw_fetcher.assert_awaited_once_with(2)
