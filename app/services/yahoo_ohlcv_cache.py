import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

import exchange_calendars as xcals
import pandas as pd
import redis.asyncio as redis

from app.core.config import settings
from app.services.ohlcv_cache_common import (
    _acquire_lock,
    _empty_dataframe,
    _release_lock,
    _upsert_rows,
    create_redis_client,
    get_closed_candles_flow,
    make_keys,
    normalize_period,
)

logger = logging.getLogger(__name__)

_SUPPORTED_PERIODS = {"day", "week", "month"}

_REDIS_CLIENT: redis.Redis | None = None
_FALLBACK_COUNT = 0


# ---------------------------------------------------------------------------
# Service-specific helpers
# ---------------------------------------------------------------------------


def _normalize_period_local(period: str) -> str:
    return normalize_period(period, _SUPPORTED_PERIODS)


def _normalize_now_utc(now: datetime | None) -> datetime:
    base_now = now or datetime.now(UTC)
    if base_now.tzinfo is None:
        return base_now.replace(tzinfo=UTC)
    return base_now.astimezone(UTC)


def _get_xnys_calendar():
    return xcals.get_calendar("XNYS")


def _recent_sessions(
    now_utc: datetime,
    lookback_days: int = 120,
    lookahead_days: int = 62,
) -> list[tuple[date, datetime]]:
    calendar = _get_xnys_calendar()
    start = pd.Timestamp(now_utc.date() - timedelta(days=lookback_days))
    end = pd.Timestamp(now_utc.date() + timedelta(days=lookahead_days))
    schedule = calendar.schedule.loc[start:end]

    sessions: list[tuple[date, datetime]] = []
    for session, row in schedule.iterrows():
        session_date = pd.Timestamp(session).date()
        close_ts = pd.Timestamp(row["close"])
        if close_ts.tzinfo is None:
            close_dt = close_ts.tz_localize(UTC).to_pydatetime()
        else:
            close_dt = close_ts.tz_convert(UTC).to_pydatetime()
        sessions.append((session_date, close_dt))
    return sessions


def _resolve_bucket_date(
    period: str,
    sessions: list[tuple[date, datetime]],
    now_utc: datetime,
) -> date:
    if period == "day":
        for session_date, close_ts in reversed(sessions):
            if close_ts <= now_utc:
                return session_date
        raise ValueError("No closed NYSE session available")

    bucket_map: dict[tuple[int, int], tuple[date, datetime]] = {}
    for session_date, close_ts in sessions:
        if period == "week":
            iso = session_date.isocalendar()
            bucket_key = (iso.year, iso.week)
        else:
            bucket_key = (session_date.year, session_date.month)

        first_date, max_close = bucket_map.get(bucket_key, (session_date, close_ts))
        if session_date < first_date:
            first_date = session_date
        if close_ts > max_close:
            max_close = close_ts
        bucket_map[bucket_key] = (first_date, max_close)

    last_closed_bucket_date: date | None = None
    for first_date, max_close in bucket_map.values():
        if max_close <= now_utc:
            last_closed_bucket_date = first_date

    if last_closed_bucket_date is None:
        raise ValueError("No closed NYSE session available")
    return last_closed_bucket_date


def get_last_closed_bucket_nyse(period: str, now: datetime | None = None) -> date:
    normalized = _normalize_period_local(period)
    now_utc = _normalize_now_utc(now)
    sessions = _recent_sessions(now_utc, lookback_days=120)
    if not sessions:
        raise ValueError("No NYSE sessions available")
    return _resolve_bucket_date(normalized, sessions, now_utc)


def _bucket_key(period: str, bucket_date: date) -> tuple[int, int, int]:
    normalized_period = _normalize_period_local(period)
    if normalized_period == "day":
        return (bucket_date.year, bucket_date.month, bucket_date.day)
    if normalized_period == "week":
        iso = bucket_date.isocalendar()
        return (iso.year, iso.week, 0)
    return (bucket_date.year, bucket_date.month, 0)


def _bucket_gap_count(period: str, earlier: date, later: date) -> int:
    normalized_period = _normalize_period_local(period)
    if later <= earlier:
        return 0

    if normalized_period == "day":
        return (later - earlier).days

    if normalized_period == "week":
        earlier_iso = earlier.isocalendar()
        later_iso = later.isocalendar()
        earlier_anchor = date.fromisocalendar(earlier_iso.year, earlier_iso.week, 1)
        later_anchor = date.fromisocalendar(later_iso.year, later_iso.week, 1)
        return max((later_anchor - earlier_anchor).days // 7, 0)

    month_gap = (later.year - earlier.year) * 12 + (later.month - earlier.month)
    return max(month_gap, 0)


def _make_is_cache_sufficient(period: str):
    """Create a period-aware cache-sufficiency checker (captures period via closure)."""

    def _is_cache_sufficient(
        cached_count: int,
        latest_cached_date: date | None,
        oldest_confirmed: bool,
        requested_count: int,
        target_closed_date: date,
    ) -> bool:
        has_latest_closed = latest_cached_date is not None and _bucket_key(
            period, latest_cached_date
        ) >= _bucket_key(period, target_closed_date)
        if not has_latest_closed:
            return False
        if cached_count >= requested_count:
            return True
        return oldest_confirmed

    return _is_cache_sufficient


def _make_is_latest_fresh(period: str):
    """Create a period-aware freshness checker (captures period via closure)."""

    def _is_latest_fresh(latest: date | None, target: date) -> bool:
        if latest is None:
            return False
        return _bucket_key(period, latest) >= _bucket_key(period, target)

    return _is_latest_fresh


def _keys(ticker: str, period: str = "day") -> tuple[str, str, str, str]:
    return make_keys("yahoo", ticker, period)


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
    ticker: str,
    count: int,
    period: str,
    raw_fetcher: Callable[[str, int, str, datetime | None], Awaitable[pd.DataFrame]],
) -> pd.DataFrame | None:
    normalized_period = _normalize_period_local(period)
    if not settings.yahoo_ohlcv_cache_enabled:
        return None

    normalized_ticker = str(ticker or "").strip().upper()
    if not normalized_ticker:
        return _empty_dataframe()

    requested_count = int(count)
    if requested_count <= 0:
        return _empty_dataframe()

    max_days = max(int(settings.yahoo_ohlcv_cache_max_days), 1)
    requested_count = min(requested_count, max_days)

    try:
        redis_client = await _get_redis_client()
        dates_key, rows_key, meta_key, lock_key = _keys(
            normalized_ticker, normalized_period
        )
        target_closed_date = get_last_closed_bucket_nyse(normalized_period)

        return await get_closed_candles_flow(
            redis_client=redis_client,
            dates_key=dates_key,
            rows_key=rows_key,
            meta_key=meta_key,
            lock_key=lock_key,
            symbol=normalized_ticker,
            period=normalized_period,
            requested_count=requested_count,
            max_days=max_days,
            lock_ttl=settings.yahoo_ohlcv_cache_lock_ttl_seconds,
            target_closed_date=target_closed_date,
            raw_fetcher=raw_fetcher,
            fetcher_symbol_kwarg="ticker",
            is_latest_fresh_fn=_make_is_latest_fresh(normalized_period),
            bucket_gap_fn=_bucket_gap_count,
            is_sufficient_fn=_make_is_cache_sufficient(normalized_period),
            acquire_lock_fn=_acquire_lock,
            release_lock_fn=_release_lock,
            sleep_fn=asyncio.sleep,
            log_prefix="yahoo_ohlcv_cache",
            meta_date_field="last_closed_bucket",
        )
    except Exception as exc:
        global _FALLBACK_COUNT
        _FALLBACK_COUNT += 1
        logger.warning(
            "yahoo_ohlcv_cache fallback ticker=%s period=%s fallback_count=%d error=%s",
            normalized_ticker,
            normalized_period,
            _FALLBACK_COUNT,
            exc,
        )
        return None


async def get_closed_daily_candles(
    ticker: str,
    count: int,
    raw_fetcher: Callable[[str, int, str, datetime | None], Awaitable[pd.DataFrame]],
) -> pd.DataFrame | None:
    return await get_closed_candles(
        ticker=ticker,
        count=count,
        period="day",
        raw_fetcher=raw_fetcher,
    )


__all__ = [
    "close_ohlcv_cache_redis",
    "get_closed_candles",
    "get_closed_daily_candles",
    "get_last_closed_bucket_nyse",
]
