import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta, timezone

import pandas as pd
import redis.asyncio as redis

import app.services.brokers.upbit.client as upbit_service
from app.core.config import settings
from app.services.ohlcv_cache_common import (
    _acquire_lock,
    _empty_dataframe,
    _enforce_retention_limit,
    _normalize_bool,
    _read_cache_status,
    _read_cached_rows,
    _read_latest_date,
    _read_oldest_date,
    _refresh_meta,
    _release_lock,
    _upsert_rows,
)

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_SUPPORTED_PERIODS = {"day", "week", "month"}

_REDIS_CLIENT: redis.Redis | None = None
_FALLBACK_COUNT = 0


def get_target_closed_date_kst(now: datetime | None = None) -> date:
    return get_last_closed_bucket_kst("day", now)


def get_last_closed_bucket_kst(period: str, now: datetime | None = None) -> date:
    normalized_period = _normalize_period(period)

    base_now = now or datetime.now(UTC)
    if base_now.tzinfo is None:
        base_now = base_now.replace(tzinfo=UTC)

    kst_now = base_now.astimezone(_KST)
    anchor_date = kst_now.date()
    if kst_now.time() < time(9, 0):
        anchor_date -= timedelta(days=1)

    if normalized_period == "day":
        return anchor_date - timedelta(days=1)

    if normalized_period == "week":
        current_week_start = anchor_date - timedelta(days=anchor_date.weekday())
        return current_week_start - timedelta(days=7)

    current_month_start = anchor_date.replace(day=1)
    previous_month_last_day = current_month_start - timedelta(days=1)
    return previous_month_last_day.replace(day=1)


def _base_key(market: str, period: str = "day") -> str:
    normalized_period = str(period or "day").strip().lower()
    return f"upbit:ohlcv:{normalized_period}:v1:{market}"


def _keys(market: str, period: str = "day") -> tuple[str, str, str, str]:
    base = _base_key(market, period)
    return f"{base}:dates", f"{base}:rows", f"{base}:meta", f"{base}:lock"


def _normalize_period(period: str) -> str:
    normalized = str(period or "").strip().lower()
    if normalized not in _SUPPORTED_PERIODS:
        raise ValueError(f"period must be one of {sorted(_SUPPORTED_PERIODS)}")
    return normalized


def _bucket_gap_count(period: str, earlier: date, later: date) -> int:
    normalized_period = _normalize_period(period)
    if later <= earlier:
        return 0

    if normalized_period == "day":
        return (later - earlier).days

    if normalized_period == "week":
        return max((later - earlier).days // 7, 0)

    month_gap = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    return max(month_gap, 0)


async def _get_redis_client() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = redis.from_url(
            settings.get_redis_url(),
            max_connections=settings.redis_max_connections,
            socket_timeout=settings.redis_socket_timeout,
            socket_connect_timeout=settings.redis_socket_connect_timeout,
            decode_responses=True,
        )
    return _REDIS_CLIENT


async def close_ohlcv_cache_redis() -> None:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        await _REDIS_CLIENT.close()
        _REDIS_CLIENT = None


def _is_cache_sufficient(
    cached_count: int,
    latest_cached_date: date | None,
    oldest_confirmed: bool,
    requested_count: int,
    target_closed_date: date,
) -> bool:
    has_latest_closed = (
        latest_cached_date is not None and latest_cached_date >= target_closed_date
    )
    if not has_latest_closed:
        return False
    if cached_count >= requested_count:
        return True
    return oldest_confirmed


async def _backfill_until_satisfied(
    redis_client: redis.Redis,
    market: str,
    period: str,
    raw_fetcher: Callable[
        [str, int, str, datetime | None],
        Awaitable[pd.DataFrame],
    ],
    requested_count: int,
    target_closed_date: date,
    dates_key: str,
    rows_key: str,
    meta_key: str,
    max_days: int,
) -> None:
    meta = await redis_client.hgetall(meta_key)
    oldest_confirmed = _normalize_bool(meta.get("oldest_confirmed"))

    # Stage A: keep cache fresh by filling newest closed candles first.
    while True:
        latest_cached_date = await _read_latest_date(redis_client, dates_key)
        if latest_cached_date is not None and latest_cached_date >= target_closed_date:
            break

        if latest_cached_date is None:
            batch_size = min(max(requested_count, 1), 200)
        else:
            missing_latest_buckets = _bucket_gap_count(
                period,
                latest_cached_date,
                target_closed_date,
            )
            batch_size = min(max(missing_latest_buckets, 1), 200)

        fetched = await raw_fetcher(
            market=market,
            days=batch_size,
            period=period,
            end_date=datetime.combine(target_closed_date, time(23, 59, 59)),
        )
        if fetched.empty or "date" not in fetched.columns:
            break

        fetched = fetched[fetched["date"] <= target_closed_date]
        if fetched.empty:
            break

        inserted_count = await _upsert_rows(redis_client, dates_key, rows_key, fetched)
        logger.info(
            "upbit_ohlcv_cache forward_fill market=%s rows=%d requested=%d",
            market,
            inserted_count,
            batch_size,
        )

        trimmed_count = await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )
        if trimmed_count > 0:
            logger.info(
                "upbit_ohlcv_cache trimmed market=%s removed=%d",
                market,
                trimmed_count,
            )

        latest_after = await _read_latest_date(redis_client, dates_key)
        if latest_cached_date is not None and (
            latest_after is None or latest_after <= latest_cached_date
        ):
            break

        if len(fetched) < batch_size:
            break

    # Stage B: fetch older candles only when depth is insufficient.
    while True:
        cached_count, latest_cached_date, oldest_confirmed = await _read_cache_status(
            redis_client,
            dates_key,
            meta_key,
            target_closed_date,
        )
        if _is_cache_sufficient(
            cached_count,
            latest_cached_date,
            oldest_confirmed,
            requested_count,
            target_closed_date,
        ):
            await _refresh_meta(
                redis_client,
                dates_key,
                meta_key,
                target_closed_date,
                oldest_confirmed,
            )
            return
        if oldest_confirmed:
            break

        earliest_cached_date = await _read_oldest_date(redis_client, dates_key)
        batch_end_date = (
            earliest_cached_date - timedelta(days=1)
            if earliest_cached_date is not None
            else target_closed_date
        )
        if batch_end_date > target_closed_date:
            batch_end_date = target_closed_date

        remaining = requested_count - cached_count
        batch_size = min(max(remaining, 1), 200)

        fetched = await raw_fetcher(
            market=market,
            days=batch_size,
            period=period,
            end_date=datetime.combine(batch_end_date, time(23, 59, 59)),
        )
        if fetched.empty or "date" not in fetched.columns:
            if not oldest_confirmed:
                logger.info(
                    "upbit_ohlcv_cache oldest_confirmed enabled market=%s reason=empty_batch",
                    market,
                )
            oldest_confirmed = True
            break

        fetched = fetched[fetched["date"] <= target_closed_date]
        if fetched.empty:
            if not oldest_confirmed:
                logger.info(
                    "upbit_ohlcv_cache oldest_confirmed enabled market=%s reason=no_closed_rows",
                    market,
                )
            oldest_confirmed = True
            break

        inserted_count = await _upsert_rows(redis_client, dates_key, rows_key, fetched)
        logger.info(
            "upbit_ohlcv_cache backfill market=%s rows=%d requested=%d",
            market,
            inserted_count,
            batch_size,
        )

        trimmed_count = await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )
        if trimmed_count > 0:
            logger.info(
                "upbit_ohlcv_cache trimmed market=%s removed=%d",
                market,
                trimmed_count,
            )

        if len(fetched) < batch_size:
            if not oldest_confirmed:
                logger.info(
                    "upbit_ohlcv_cache oldest_confirmed enabled market=%s reason=short_batch returned=%d requested=%d",
                    market,
                    len(fetched),
                    batch_size,
                )
            oldest_confirmed = True
            break

    await _refresh_meta(
        redis_client,
        dates_key,
        meta_key,
        target_closed_date,
        oldest_confirmed,
    )


async def get_closed_candles(
    market: str,
    count: int,
    period: str,
    raw_fetcher: Callable[
        [str, int, str, datetime | None],
        Awaitable[pd.DataFrame],
    ]
    | None = None,
) -> pd.DataFrame | None:
    normalized_period = _normalize_period(period)
    if not settings.upbit_ohlcv_cache_enabled:
        return None

    normalized_market = str(market or "").strip().upper()
    if not normalized_market:
        return _empty_dataframe()

    requested_count = int(count)
    if requested_count <= 0:
        return _empty_dataframe()

    max_days = max(int(settings.upbit_ohlcv_cache_max_days), 1)
    requested_count = min(requested_count, max_days)
    fetcher = raw_fetcher or upbit_service.fetch_ohlcv

    try:
        redis_client = await _get_redis_client()
        dates_key, rows_key, meta_key, lock_key = _keys(
            normalized_market, normalized_period
        )
        if normalized_period == "day":
            target_closed_date = get_target_closed_date_kst()
        else:
            target_closed_date = get_last_closed_bucket_kst(normalized_period)

        trimmed_count = await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )
        if trimmed_count > 0:
            logger.info(
                "upbit_ohlcv_cache trimmed market=%s period=%s removed=%d",
                normalized_market,
                normalized_period,
                trimmed_count,
            )

        cached = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_closed_date,
            requested_count,
        )
        (
            cached_count,
            latest_cached_date,
            oldest_confirmed,
        ) = await _read_cache_status(
            redis_client,
            dates_key,
            meta_key,
            target_closed_date,
        )
        if _is_cache_sufficient(
            cached_count,
            latest_cached_date,
            oldest_confirmed,
            requested_count,
            target_closed_date,
        ):
            await _refresh_meta(
                redis_client,
                dates_key,
                meta_key,
                target_closed_date,
                oldest_confirmed,
            )
            logger.info(
                "upbit_ohlcv_cache hit market=%s period=%s cached=%d requested=%d",
                normalized_market,
                normalized_period,
                len(cached),
                requested_count,
            )
            return cached.tail(requested_count).reset_index(drop=True)

        logger.info(
            "upbit_ohlcv_cache miss market=%s period=%s cached=%d requested=%d",
            normalized_market,
            normalized_period,
            len(cached),
            requested_count,
        )

        lock_token = await _acquire_lock(
            redis_client,
            lock_key,
            settings.upbit_ohlcv_cache_lock_ttl_seconds,
        )
        if lock_token is None:
            for _ in range(2):
                await asyncio.sleep(0.1)
                lock_token = await _acquire_lock(
                    redis_client,
                    lock_key,
                    settings.upbit_ohlcv_cache_lock_ttl_seconds,
                )
                if lock_token is not None:
                    break
            if lock_token is None:
                refreshed_cached = await _read_cached_rows(
                    redis_client,
                    dates_key,
                    rows_key,
                    target_closed_date,
                    requested_count,
                )
                (
                    refreshed_count,
                    refreshed_latest_date,
                    refreshed_oldest_confirmed,
                ) = await _read_cache_status(
                    redis_client,
                    dates_key,
                    meta_key,
                    target_closed_date,
                )
                if _is_cache_sufficient(
                    refreshed_count,
                    refreshed_latest_date,
                    refreshed_oldest_confirmed,
                    requested_count,
                    target_closed_date,
                ):
                    return refreshed_cached.tail(requested_count).reset_index(drop=True)
                return None

        try:
            await _backfill_until_satisfied(
                redis_client,
                normalized_market,
                normalized_period,
                fetcher,
                requested_count,
                target_closed_date,
                dates_key,
                rows_key,
                meta_key,
                max_days,
            )
        finally:
            await _release_lock(redis_client, lock_key, lock_token)

        final_rows = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_closed_date,
            requested_count,
        )
        return final_rows.tail(requested_count).reset_index(drop=True)

    except Exception as exc:
        global _FALLBACK_COUNT
        _FALLBACK_COUNT += 1
        logger.warning(
            "upbit_ohlcv_cache fallback market=%s period=%s fallback_count=%d error=%s",
            normalized_market,
            normalized_period,
            _FALLBACK_COUNT,
            exc,
        )
        return None


async def get_closed_daily_candles(
    market: str,
    count: int,
    raw_fetcher: Callable[
        [str, int, str, datetime | None],
        Awaitable[pd.DataFrame],
    ]
    | None = None,
) -> pd.DataFrame | None:
    return await get_closed_candles(
        market=market,
        count=count,
        period="day",
        raw_fetcher=raw_fetcher,
    )


__all__ = [
    "close_ohlcv_cache_redis",
    "get_closed_candles",
    "get_closed_daily_candles",
    "get_last_closed_bucket_kst",
    "get_target_closed_date_kst",
]
