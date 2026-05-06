import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache

import exchange_calendars as xcals
import pandas as pd
import redis.asyncio as redis

from app.core.config import settings
from app.core.log_sanitize import safe_log_value
from app.core.timezone import KST, now_kst
from app.services.ohlcv_cache_common import (
    _acquire_lock,
    _enforce_retention_limit,
    _release_lock,
    _to_json_value,
    acquire_lock_with_retry,
    create_redis_client,
    make_keys,
    normalize_period,
)

logger = logging.getLogger(__name__)

_SUPPORTED_PERIODS = {"day", "1h"}
_DAY_COLUMNS = ["date", "open", "high", "low", "close", "volume", "value"]
_KRX_DAILY_CACHE_CUTOFF = time(15, 35)
_HOURLY_COLUMNS = [
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
]

_REDIS_CLIENT: redis.Redis | None = None
_FALLBACK_COUNT = 0


def _normalize_period_local(period: str) -> str:
    return normalize_period(period, _SUPPORTED_PERIODS)


def _base_key(symbol: str, period: str, route: str | None = None) -> str:
    from app.services.ohlcv_cache_common import make_base_key

    return make_base_key("kis", symbol, period, route)


def _keys(
    symbol: str, period: str, route: str | None = None
) -> tuple[str, str, str, str]:
    return make_keys("kis", symbol, period, route)


def _empty_dataframe(period: str) -> pd.DataFrame:
    if period == "1h":
        return pd.DataFrame(columns=_HOURLY_COLUMNS)
    return pd.DataFrame(columns=_DAY_COLUMNS)


def _get_retention_limit(period: str) -> int:
    if period == "1h":
        return max(int(settings.kis_ohlcv_cache_max_hours), 1)
    return max(int(settings.kis_ohlcv_cache_max_days), 1)


def _coerce_datetime(value: object) -> pd.Timestamp | None:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    if not isinstance(parsed, pd.Timestamp):
        return None
    return parsed


def _coerce_kst_datetime(now: datetime | None = None) -> datetime:
    current = now or now_kst()
    if current.tzinfo is None:
        return current.replace(tzinfo=KST)
    return current.astimezone(KST)


@lru_cache(maxsize=1)
def _get_xkrx_calendar():
    return xcals.get_calendar("XKRX")


def _is_session_day_kst(target_day: date) -> bool:
    calendar = _get_xkrx_calendar()
    return bool(calendar.is_session(pd.Timestamp(target_day)))


def _latest_session_day_on_or_before(target_day: date) -> date | None:
    calendar = _get_xkrx_calendar()
    start = pd.Timestamp(target_day - timedelta(days=30))
    end = pd.Timestamp(target_day)
    sessions = calendar.sessions_in_range(start, end)
    if len(sessions) == 0:
        return None
    return pd.Timestamp(sessions[-1]).date()


def _latest_session_day_before(target_day: date) -> date | None:
    return _latest_session_day_on_or_before(target_day - timedelta(days=1))


def _canonicalize_frame(period: str, frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return _empty_dataframe(period)

    if period == "day":
        out = frame.copy()
        if "date" not in out.columns and "datetime" in out.columns:
            out["date"] = pd.to_datetime(out["datetime"], errors="coerce").dt.date
        out["date"] = pd.to_datetime(out.get("date"), errors="coerce").dt.date
        out = out.dropna(subset=["date"])
        for col in _DAY_COLUMNS:
            if col not in out.columns:
                out[col] = None
        out = out.loc[:, _DAY_COLUMNS].drop_duplicates(subset=["date"], keep="last")
        return out.sort_values("date").reset_index(drop=True)

    out = frame.copy()
    if "datetime" not in out.columns:
        if "date" in out.columns and "time" in out.columns:
            out["datetime"] = pd.to_datetime(
                out["date"].astype(str) + " " + out["time"].astype(str),
                errors="coerce",
            )
        else:
            return _empty_dataframe(period)

    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    if out.empty:
        return _empty_dataframe(period)

    out["datetime"] = out["datetime"].dt.floor("60min")
    out["date"] = out["datetime"].dt.date
    out["time"] = out["datetime"].dt.time
    for col in _HOURLY_COLUMNS:
        if col not in out.columns:
            out[col] = None
    out = out.loc[:, _HOURLY_COLUMNS].drop_duplicates(subset=["datetime"], keep="last")
    return out.sort_values("datetime").reset_index(drop=True)


def _is_cache_fresh(
    period: str, frame: pd.DataFrame, now: datetime | None = None
) -> bool:
    if frame.empty:
        return False

    if period == "day":
        current_kst = _coerce_kst_datetime(now)
        latest = pd.to_datetime(frame.get("date"), errors="coerce").max()
        if pd.isna(latest):
            return False
        latest_date = latest.date()
        current_date = current_kst.date()
        if latest_date > current_date:
            return True

        current_time = current_kst.time()
        is_session_day = _is_session_day_kst(current_date)
        if not is_session_day:
            return latest_date == _latest_session_day_on_or_before(current_date)

        if current_time < time(9, 0):
            previous_session_day = _latest_session_day_before(current_date)
            return latest_date == current_date or latest_date == previous_session_day

        if current_time < _KRX_DAILY_CACHE_CUTOFF:
            return False

        return latest_date == current_date

    current = pd.Timestamp(now or datetime.now())
    if pd.isna(current):
        return False

    latest = pd.to_datetime(frame.get("datetime"), errors="coerce").max()
    if pd.isna(latest):
        return False
    return latest.floor("60min") >= current.floor("60min")


def _field_and_score(period: str, row: pd.Series) -> tuple[str, int] | None:
    if period == "day":
        value = row.get("date")
        row_date = pd.to_datetime(value, errors="coerce")
        if pd.isna(row_date):
            return None
        row_date = row_date.date()
        field = row_date.isoformat()
        score = int(
            datetime(
                row_date.year, row_date.month, row_date.day, tzinfo=UTC
            ).timestamp()
        )
        return field, score

    dt = _coerce_datetime(row.get("datetime"))
    if dt is None:
        return None
    bucket = dt.floor("60min")
    field = bucket.isoformat()
    score = int(bucket.value // 1_000_000_000)
    return field, score


def _parse_cached_row(
    period: str, field: str, payload: str
) -> dict[str, object] | None:
    try:
        parsed = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None

    if period == "day":
        raw_date = parsed.get("date", field)
        parsed_date = pd.to_datetime(raw_date, errors="coerce")
        if pd.isna(parsed_date):
            return None
        row: dict[str, object] = {"date": parsed_date.date()}
        for col in _DAY_COLUMNS:
            if col == "date":
                continue
            row[col] = parsed.get(col)
        return row

    raw_dt = parsed.get("datetime", field)
    parsed_dt = _coerce_datetime(raw_dt)
    if parsed_dt is None:
        return None
    row: dict[str, object] = {
        "datetime": parsed_dt,
        "date": parsed_dt.date(),
        "time": parsed_dt.time(),
    }
    for col in _HOURLY_COLUMNS:
        if col in {"datetime", "date", "time"}:
            continue
        row[col] = parsed.get(col)
    return row


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


async def _read_cached_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    count: int,
    period: str,
) -> pd.DataFrame:
    if count <= 0:
        return _empty_dataframe(period)

    fields = await redis_client.zrevrange(dates_key, 0, count - 1)
    if not fields:
        return _empty_dataframe(period)

    payloads = await redis_client.hmget(rows_key, fields)
    rows: list[dict[str, object]] = []
    for field, payload in zip(fields, payloads, strict=False):
        if not payload:
            continue
        parsed = _parse_cached_row(period, field, payload)
        if parsed is not None:
            rows.append(parsed)

    if not rows:
        return _empty_dataframe(period)

    out = pd.DataFrame(rows)
    if period == "1h":
        for col in _HOURLY_COLUMNS:
            if col not in out.columns:
                out[col] = None
        return (
            out.loc[:, _HOURLY_COLUMNS].sort_values("datetime").reset_index(drop=True)
        )

    for col in _DAY_COLUMNS:
        if col not in out.columns:
            out[col] = None
    return out.loc[:, _DAY_COLUMNS].sort_values("date").reset_index(drop=True)


async def _upsert_rows(
    redis_client: redis.Redis,
    dates_key: str,
    rows_key: str,
    frame: pd.DataFrame,
    period: str,
) -> int:
    canonical = _canonicalize_frame(period, frame)
    if canonical.empty:
        return 0

    zadd_mapping: dict[str, int] = {}
    hset_mapping: dict[str, str] = {}

    for _, row in canonical.iterrows():
        pair = _field_and_score(period, row)
        if pair is None:
            continue
        field, score = pair

        if period == "day":
            payload = {
                "date": field,
                "open": _to_json_value(row.get("open")),
                "high": _to_json_value(row.get("high")),
                "low": _to_json_value(row.get("low")),
                "close": _to_json_value(row.get("close")),
                "volume": _to_json_value(row.get("volume")),
                "value": _to_json_value(row.get("value")),
            }
        else:
            payload = {
                "datetime": field,
                "date": str(row.get("date")),
                "time": str(row.get("time")),
                "open": _to_json_value(row.get("open")),
                "high": _to_json_value(row.get("high")),
                "low": _to_json_value(row.get("low")),
                "close": _to_json_value(row.get("close")),
                "volume": _to_json_value(row.get("volume")),
                "value": _to_json_value(row.get("value")),
            }

        zadd_mapping[field] = score
        hset_mapping[field] = json.dumps(payload)

    if not zadd_mapping:
        return 0

    pipe = redis_client.pipeline(transaction=True)
    pipe.zadd(dates_key, zadd_mapping)
    pipe.hset(rows_key, mapping=hset_mapping)
    await pipe.execute()
    return len(zadd_mapping)


async def get_candles(
    symbol: str,
    count: int,
    period: str,
    raw_fetcher: Callable[[int], Awaitable[pd.DataFrame]],
    route: str | None = None,
) -> pd.DataFrame:
    normalized_period = _normalize_period_local(period)
    requested_count = int(count)
    if requested_count <= 0:
        return _empty_dataframe(normalized_period)

    retention_limit = _get_retention_limit(normalized_period)
    requested_count = min(requested_count, retention_limit)

    if not settings.kis_ohlcv_cache_enabled:
        raw = await raw_fetcher(requested_count)
        return (
            _canonicalize_frame(normalized_period, raw)
            .tail(requested_count)
            .reset_index(drop=True)
        )

    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return _empty_dataframe(normalized_period)

    try:
        redis_client = await _get_redis_client()
        dates_key, rows_key, _, lock_key = _keys(
            normalized_symbol,
            normalized_period,
            route,
        )

        await _enforce_retention_limit(
            redis_client, dates_key, rows_key, retention_limit
        )
        cached = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            requested_count,
            normalized_period,
        )
        if len(cached) >= requested_count and _is_cache_fresh(
            normalized_period, cached
        ):
            return cached.tail(requested_count).reset_index(drop=True)

        lock_token = await acquire_lock_with_retry(
            redis_client,
            lock_key,
            settings.kis_ohlcv_cache_lock_ttl_seconds,
            _acquire_lock,
            asyncio.sleep,
        )
        if lock_token is None:
            refreshed = await _read_cached_rows(
                redis_client,
                dates_key,
                rows_key,
                requested_count,
                normalized_period,
            )
            if len(refreshed) >= requested_count and _is_cache_fresh(
                normalized_period, refreshed
            ):
                return refreshed.tail(requested_count).reset_index(drop=True)
            raw_fallback = await raw_fetcher(requested_count)
            return (
                _canonicalize_frame(normalized_period, raw_fallback)
                .tail(requested_count)
                .reset_index(drop=True)
            )

        raw_frame = _empty_dataframe(normalized_period)
        try:
            raw_frame = _canonicalize_frame(
                normalized_period, await raw_fetcher(requested_count)
            )
            if not raw_frame.empty:
                await _upsert_rows(
                    redis_client,
                    dates_key,
                    rows_key,
                    raw_frame,
                    normalized_period,
                )
                await _enforce_retention_limit(
                    redis_client,
                    dates_key,
                    rows_key,
                    retention_limit,
                )
        finally:
            await _release_lock(redis_client, lock_key, lock_token)

        final_rows = await _read_cached_rows(
            redis_client,
            dates_key,
            rows_key,
            requested_count,
            normalized_period,
        )
        if not final_rows.empty:
            return final_rows.tail(requested_count).reset_index(drop=True)
        return raw_frame.tail(requested_count).reset_index(drop=True)
    except Exception as exc:
        global _FALLBACK_COUNT
        _FALLBACK_COUNT += 1
        logger.warning(
            "kis_ohlcv_cache fallback symbol=%s period=%s fallback_count=%d error=%s",
            safe_log_value(normalized_symbol),
            safe_log_value(normalized_period),
            _FALLBACK_COUNT,
            exc,
        )
        raw = await raw_fetcher(requested_count)
        return (
            _canonicalize_frame(normalized_period, raw)
            .tail(requested_count)
            .reset_index(drop=True)
        )


__all__ = [
    "close_ohlcv_cache_redis",
    "get_candles",
]
