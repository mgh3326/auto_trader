# app/services/ohlcv_cache_common.py
"""Shared utilities for OHLCV Redis cache modules.

Upbit, Yahoo, KIS 캐시 모듈이 공유하는 순수 함수 모음.
각 서비스 모듈에서 `from app.services.ohlcv_cache_common import ...` 로 사용.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

import pandas as pd
import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_EMPTY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]


# ---------------------------------------------------------------------------
# Pure utilities
# ---------------------------------------------------------------------------


def _to_json_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value


def _normalize_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _epoch_day(value: date) -> int:
    return int(
        datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() // 86400
    )


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_EMPTY_COLUMNS)


# ---------------------------------------------------------------------------
# Generic parameterized utilities
# ---------------------------------------------------------------------------


def normalize_period(period: str, supported: set[str]) -> str:
    """Validate and normalize a period string against a set of supported values."""
    normalized = str(period or "").strip().lower()
    if normalized not in supported:
        raise ValueError(f"period must be one of {sorted(supported)}")
    return normalized


def make_base_key(
    prefix: str,
    identifier: str,
    period: str,
    extra: str | None = None,
) -> str:
    """Build a Redis base key: '{prefix}:ohlcv:{period}:v1:{ID}[:EXTRA]'."""
    norm_id = str(identifier or "").strip().upper()
    norm_period = str(period or "").strip().lower()
    base = f"{prefix}:ohlcv:{norm_period}:v1:{norm_id}"
    norm_extra = str(extra or "").strip().upper()
    if norm_extra:
        return f"{base}:{norm_extra}"
    return base


def make_keys(
    prefix: str,
    identifier: str,
    period: str,
    extra: str | None = None,
) -> tuple[str, str, str, str]:
    """Return (dates_key, rows_key, meta_key, lock_key) for a cache entry."""
    base = make_base_key(prefix, identifier, period, extra)
    return f"{base}:dates", f"{base}:rows", f"{base}:meta", f"{base}:lock"


async def create_redis_client() -> redis.Redis:
    """Create an async Redis client using application settings."""
    return redis.from_url(
        settings.get_redis_url(),
        max_connections=settings.redis_max_connections,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        decode_responses=True,
    )


# ---------------------------------------------------------------------------
# Redis lock
# ---------------------------------------------------------------------------


async def _acquire_lock(
    redis_client: redis.Redis,
    lock_key: str,
    ttl_seconds: int,
) -> str | None:
    lock_token = f"{uuid.uuid4()}"
    acquired = await redis_client.set(
        lock_key,
        lock_token,
        nx=True,
        ex=max(int(ttl_seconds), 1),
    )
    if acquired:
        return lock_token
    return None


async def _release_lock(
    redis_client: redis.Redis,
    lock_key: str,
    lock_token: str,
) -> None:
    release_script = """
    if redis.call('GET', KEYS[1]) == ARGV[1] then
        return redis.call('DEL', KEYS[1])
    else
        return 0
    end
    """
    try:
        await redis_client.eval(release_script, 1, lock_key, lock_token)
    except Exception:
        return


async def acquire_lock_with_retry(
    redis_client: redis.Redis,
    lock_key: str,
    ttl_seconds: int,
    acquire_fn: Callable[..., Awaitable[str | None]],
    sleep_fn: Callable[..., Awaitable[None]],
    retries: int = 2,
    delay: float = 0.1,
) -> str | None:
    """Try to acquire a distributed lock, retrying up to *retries* times."""
    token = await acquire_fn(redis_client, lock_key, ttl_seconds)
    if token is not None:
        return token
    for _ in range(retries):
        await sleep_fn(delay)
        token = await acquire_fn(redis_client, lock_key, ttl_seconds)
        if token is not None:
            return token
    return None


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


async def _enforce_retention_limit(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    max_items: int,
) -> int:
    if max_items <= 0:
        return 0

    total_count = int(await redis_client.zcard(dates_key))
    overflow = total_count - max_items
    if overflow <= 0:
        return 0

    stale_fields = await redis_client.zrange(dates_key, 0, overflow - 1)
    if not stale_fields:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zremrangebyrank(dates_key, 0, overflow - 1)
    pipeline.hdel(rows_key, *stale_fields)
    await pipeline.execute()
    return len(stale_fields)


# ---------------------------------------------------------------------------
# Date helpers (upbit/yahoo shared)
# ---------------------------------------------------------------------------


async def _read_oldest_date(redis_client: redis.Redis, dates_key: str) -> date | None:
    oldest_dates = await redis_client.zrange(dates_key, 0, 0)
    if not oldest_dates:
        return None
    try:
        return date.fromisoformat(oldest_dates[0])
    except ValueError:
        return None


async def _read_latest_date(redis_client: redis.Redis, dates_key: str) -> date | None:
    latest_dates = await redis_client.zrevrangebyscore(
        dates_key,
        "+inf",
        "-inf",
        start=0,
        num=1,
    )
    if not latest_dates:
        return None
    try:
        return date.fromisoformat(latest_dates[0])
    except ValueError:
        return None


async def _read_cache_status(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
) -> tuple[int, date | None, bool]:
    cached_count = int(
        await redis_client.zcount(dates_key, "-inf", _epoch_day(target_closed_date))
    )
    latest_cached_date = await _read_latest_date(redis_client, dates_key)
    meta = await redis_client.hgetall(meta_key)
    oldest_confirmed = _normalize_bool(meta.get("oldest_confirmed"))
    return cached_count, latest_cached_date, oldest_confirmed


# ---------------------------------------------------------------------------
# Daily read/write (upbit/yahoo shared)
# ---------------------------------------------------------------------------


async def _read_cached_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    target_closed_date: date,
    count: int,
) -> pd.DataFrame:
    if count <= 0:
        return _empty_dataframe()

    date_fields = await redis_client.zrevrangebyscore(
        dates_key,
        _epoch_day(target_closed_date),
        "-inf",
        start=0,
        num=count,
    )
    if not date_fields:
        return _empty_dataframe()

    row_payloads = await redis_client.hmget(rows_key, date_fields)
    rows: list[dict[str, object]] = []
    for field, payload in zip(date_fields, row_payloads, strict=False):
        if not payload:
            continue
        try:
            parsed = json.loads(payload)
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(parsed, dict):
            continue
        date_value = parsed.get("date", field)
        try:
            parsed["date"] = date.fromisoformat(str(date_value))
        except ValueError:
            continue
        rows.append(parsed)

    if not rows:
        return _empty_dataframe()

    frame = pd.DataFrame(rows)
    for column in _EMPTY_COLUMNS:
        if column not in frame.columns:
            frame[column] = None

    return frame.loc[:, _EMPTY_COLUMNS].sort_values("date").reset_index(drop=True)


async def _upsert_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    frame: pd.DataFrame,
) -> int:
    if frame.empty:
        return 0

    zadd_mapping: dict[str, int] = {}
    hset_mapping: dict[str, str] = {}

    for row in frame.itertuples(index=False):
        row_date = getattr(row, "date", None)
        if row_date is None:
            continue
        if not isinstance(row_date, date):
            try:
                row_date = pd.to_datetime(row_date).date()
            except Exception:
                continue

        field = row_date.isoformat()
        zadd_mapping[field] = _epoch_day(row_date)
        payload = {
            "date": field,
            "open": _to_json_value(getattr(row, "open", None)),
            "high": _to_json_value(getattr(row, "high", None)),
            "low": _to_json_value(getattr(row, "low", None)),
            "close": _to_json_value(getattr(row, "close", None)),
            "volume": _to_json_value(getattr(row, "volume", None)),
            "value": _to_json_value(getattr(row, "value", None)),
        }
        hset_mapping[field] = json.dumps(payload)

    if not zadd_mapping or not hset_mapping:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zadd(dates_key, zadd_mapping)
    pipeline.hset(rows_key, mapping=hset_mapping)
    await pipeline.execute()
    return len(zadd_mapping)


# ---------------------------------------------------------------------------
# Meta refresh (upbit/yahoo shared, parameterized)
# ---------------------------------------------------------------------------


async def _refresh_meta(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
    oldest_confirmed: bool,
    meta_date_field: str = "last_closed_date",
) -> None:
    oldest_date = await _read_oldest_date(redis_client, dates_key)
    mapping = {
        meta_date_field: target_closed_date.isoformat(),
        "oldest_date": oldest_date.isoformat() if oldest_date else "",
        "oldest_confirmed": "true" if oldest_confirmed else "false",
        "last_sync_ts": str(int(datetime.now(UTC).timestamp())),
    }
    await redis_client.hset(meta_key, mapping=mapping)
