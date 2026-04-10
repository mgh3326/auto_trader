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
    _release_lock,
    _upsert_rows,  # noqa: F401
    acquire_lock_with_retry,  # noqa: F401
    create_redis_client,
    get_closed_candles_flow,
    make_keys,
    normalize_period,
)

logger = logging.getLogger(__name__)

_KST = timezone(timedelta(hours=9))
_SUPPORTED_PERIODS = {"day", "week", "month"}

_REDIS_CLIENT: redis.Redis | None = None
_FALLBACK_COUNT = 0


# ---------------------------------------------------------------------------
# Service-specific helpers
# ---------------------------------------------------------------------------


def _normalize_period_local(period: str) -> str:
    return normalize_period(period, _SUPPORTED_PERIODS)


def _base_key(market: str, period: str = "day") -> str:
    from app.services.ohlcv_cache_common import make_base_key

    return make_base_key("upbit", market, period)


def _keys(market: str, period: str = "day") -> tuple[str, str, str, str]:
    return make_keys("upbit", market, period)


def get_target_closed_date_kst(now: datetime | None = None) -> date:
    return get_last_closed_bucket_kst("day", now)


def get_last_closed_bucket_kst(period: str, now: datetime | None = None) -> date:
    normalized_period = _normalize_period_local(period)

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


def _bucket_gap_count(period: str, earlier: date, later: date) -> int:
    normalized_period = _normalize_period_local(period)
    if later <= earlier:
        return 0

    if normalized_period == "day":
        return (later - earlier).days

    if normalized_period == "week":
        return max((later - earlier).days // 7, 0)

    month_gap = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    return max(month_gap, 0)


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


def _is_latest_fresh(latest: date | None, target: date) -> bool:
    return latest is not None and latest >= target


# ---------------------------------------------------------------------------
# Redis client management
# ---------------------------------------------------------------------------


async def _get_redis_client() -> redis.Redis:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is None:
        _REDIS_CLIENT = await create_redis_client()
    return _REDIS_CLIENT


async def close_ohlcv_cache_redis() -> None:
    global _REDIS_CLIENT
    if _REDIS_CLIENT is not None:
        await _REDIS_CLIENT.close()
        _REDIS_CLIENT = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


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
    normalized_period = _normalize_period_local(period)
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

        return await get_closed_candles_flow(
            redis_client=redis_client,
            dates_key=dates_key,
            rows_key=rows_key,
            meta_key=meta_key,
            lock_key=lock_key,
            symbol=normalized_market,
            period=normalized_period,
            requested_count=requested_count,
            max_days=max_days,
            lock_ttl=settings.upbit_ohlcv_cache_lock_ttl_seconds,
            target_closed_date=target_closed_date,
            raw_fetcher=fetcher,
            fetcher_symbol_kwarg="market",
            is_latest_fresh_fn=_is_latest_fresh,
            bucket_gap_fn=_bucket_gap_count,
            is_sufficient_fn=_is_cache_sufficient,
            acquire_lock_fn=_acquire_lock,
            release_lock_fn=_release_lock,
            sleep_fn=asyncio.sleep,
            log_prefix="upbit_ohlcv_cache",
        )
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
