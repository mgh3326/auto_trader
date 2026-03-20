import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta

import exchange_calendars as xcals
import pandas as pd
import redis.asyncio as redis

from app.core.config import settings

logger = logging.getLogger(__name__)

_EMPTY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]
_SUPPORTED_PERIODS = {"day", "week", "month"}

_REDIS_CLIENT: redis.Redis | None = None
_FALLBACK_COUNT = 0


def _normalize_period(period: str) -> str:
    normalized = str(period or "").strip().lower()
    if normalized not in _SUPPORTED_PERIODS:
        raise ValueError(f"period must be one of {sorted(_SUPPORTED_PERIODS)}")
    return normalized


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
    normalized = _normalize_period(period)
    now_utc = _normalize_now_utc(now)
    sessions = _recent_sessions(now_utc, lookback_days=120)
    if not sessions:
        raise ValueError("No NYSE sessions available")
    return _resolve_bucket_date(normalized, sessions, now_utc)


def _bucket_key(period: str, bucket_date: date) -> tuple[int, int, int]:
    normalized_period = _normalize_period(period)
    if normalized_period == "day":
        return (bucket_date.year, bucket_date.month, bucket_date.day)
    if normalized_period == "week":
        iso = bucket_date.isocalendar()
        return (iso.year, iso.week, 0)
    return (bucket_date.year, bucket_date.month, 0)


def _bucket_gap_count(period: str, earlier: date, later: date) -> int:
    normalized_period = _normalize_period(period)
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


def _epoch_day(value: date) -> int:
    return int(
        datetime(value.year, value.month, value.day, tzinfo=UTC).timestamp() // 86400
    )


def _base_key(ticker: str, period: str = "day") -> str:
    normalized_period = str(period or "day").strip().lower()
    normalized_ticker = str(ticker or "").strip().upper()
    return f"yahoo:ohlcv:{normalized_period}:v1:{normalized_ticker}"


def _keys(ticker: str, period: str = "day") -> tuple[str, str, str, str]:
    base = _base_key(ticker, period)
    return f"{base}:dates", f"{base}:rows", f"{base}:meta", f"{base}:lock"


def _normalize_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _empty_dataframe() -> pd.DataFrame:
    return pd.DataFrame(columns=_EMPTY_COLUMNS)


def _to_json_value(value: object) -> object:
    if pd.isna(value):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return value


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


async def _enforce_retention_limit(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    max_days: int,
) -> int:
    if max_days <= 0:
        return 0

    total_count = int(await redis_client.zcard(dates_key))
    overflow = total_count - max_days
    if overflow <= 0:
        return 0

    stale_dates = await redis_client.zrange(dates_key, 0, overflow - 1)
    if not stale_dates:
        return 0

    pipeline = redis_client.pipeline(transaction=True)
    pipeline.zremrangebyrank(dates_key, 0, overflow - 1)
    pipeline.hdel(rows_key, *stale_dates)
    await pipeline.execute()
    return len(stale_dates)


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


def _is_cache_sufficient(
    period: str,
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


async def _refresh_meta(
    redis_client: redis.Redis,
    dates_key: str,
    meta_key: str,
    target_closed_date: date,
    oldest_confirmed: bool,
) -> None:
    oldest_date = await _read_oldest_date(redis_client, dates_key)
    mapping = {
        "last_closed_bucket": target_closed_date.isoformat(),
        "oldest_date": oldest_date.isoformat() if oldest_date else "",
        "oldest_confirmed": "true" if oldest_confirmed else "false",
        "last_sync_ts": str(int(datetime.now(UTC).timestamp())),
    }
    await redis_client.hset(meta_key, mapping=mapping)


async def _backfill_until_satisfied(
    redis_client: redis.Redis,
    ticker: str,
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

    while True:
        latest_cached_date = await _read_latest_date(redis_client, dates_key)
        if latest_cached_date is not None and _bucket_key(
            period,
            latest_cached_date,
        ) >= _bucket_key(period, target_closed_date):
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
            ticker=ticker,
            days=batch_size,
            period=period,
            end_date=datetime.combine(target_closed_date, time(23, 59, 59)),
        )
        if fetched.empty or "date" not in fetched.columns:
            break

        fetched = fetched[fetched["date"] <= target_closed_date]
        if fetched.empty:
            break

        await _upsert_rows(redis_client, dates_key, rows_key, fetched)
        await _enforce_retention_limit(redis_client, dates_key, rows_key, max_days)

        latest_after = await _read_latest_date(redis_client, dates_key)
        if latest_cached_date is not None and (
            latest_after is None or latest_after <= latest_cached_date
        ):
            break

        if len(fetched) < batch_size:
            break

    while True:
        cached_count, latest_cached_date, oldest_confirmed = await _read_cache_status(
            redis_client,
            dates_key,
            meta_key,
            target_closed_date,
        )
        if _is_cache_sufficient(
            period,
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
            ticker=ticker,
            days=batch_size,
            period=period,
            end_date=datetime.combine(batch_end_date, time(23, 59, 59)),
        )
        if fetched.empty or "date" not in fetched.columns:
            oldest_confirmed = True
            break

        fetched = fetched[fetched["date"] <= batch_end_date]
        if fetched.empty:
            oldest_confirmed = True
            break

        inserted_count = await _upsert_rows(redis_client, dates_key, rows_key, fetched)
        await _enforce_retention_limit(redis_client, dates_key, rows_key, max_days)

        if inserted_count <= 0:
            oldest_confirmed = True
            break

        if len(fetched) < batch_size:
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
    ticker: str,
    count: int,
    period: str,
    raw_fetcher: Callable[[str, int, str, datetime | None], Awaitable[pd.DataFrame]],
) -> pd.DataFrame | None:
    normalized_period = _normalize_period(period)
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
            normalized_ticker,
            normalized_period,
        )
        target_closed_date = get_last_closed_bucket_nyse(normalized_period)

        await _enforce_retention_limit(
            redis_client,
            dates_key,
            rows_key,
            max_days,
        )

        cached = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            target_closed_date,
            requested_count,
        )
        cached_count, latest_cached_date, oldest_confirmed = await _read_cache_status(
            redis_client,
            dates_key,
            meta_key,
            target_closed_date,
        )
        if _is_cache_sufficient(
            normalized_period,
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
            return cached.tail(requested_count).reset_index(drop=True)

        lock_token = await _acquire_lock(
            redis_client,
            lock_key,
            settings.yahoo_ohlcv_cache_lock_ttl_seconds,
        )
        if lock_token is None:
            for _ in range(2):
                await asyncio.sleep(0.1)
                lock_token = await _acquire_lock(
                    redis_client,
                    lock_key,
                    settings.yahoo_ohlcv_cache_lock_ttl_seconds,
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
                    normalized_period,
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
                normalized_ticker,
                normalized_period,
                raw_fetcher,
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
