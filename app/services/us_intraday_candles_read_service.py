from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import Literal, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient
from app.services.us_symbol_universe_service import (
    USSymbolUniverseLookupError,
    get_us_exchange_by_symbol,
)

_ET = ZoneInfo("America/New_York")

logger = logging.getLogger(__name__)

SessionType = Literal["PRE_MARKET", "REGULAR", "POST_MARKET"]


@dataclass(frozen=True, slots=True)
class _IntradayPeriodConfig:
    period: str
    bucket_minutes: int
    history_table: str | None


_INTRADAY_PERIOD_CONFIGS: dict[str, _IntradayPeriodConfig] = {
    "1m": _IntradayPeriodConfig("1m", 1, None),
    "5m": _IntradayPeriodConfig("5m", 5, "public.us_candles_5m"),
    "15m": _IntradayPeriodConfig("15m", 15, "public.us_candles_15m"),
    "30m": _IntradayPeriodConfig("30m", 30, "public.us_candles_30m"),
    "1h": _IntradayPeriodConfig("1h", 60, None),
}

_INTERNAL_FRAME_COLUMNS = [
    "datetime",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
]

_OUTPUT_FRAME_COLUMNS = [
    "datetime",
    "date",
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "session",
]

_US_CANDLES_1M_QUERY = text(
    """
    SELECT time, open, high, low, close, volume, value
    FROM public.us_candles_1m
    WHERE symbol = :symbol
      AND exchange = :exchange
      AND time <= :end_time
    ORDER BY time DESC
    LIMIT :limit
    """
)

_US_CANDLES_CAGG_QUERY_TEMPLATE = """
SELECT bucket, open, high, low, close, volume, value
FROM {table}
WHERE symbol = :symbol
  AND exchange = :exchange
  AND bucket <= :end_time
ORDER BY bucket DESC
LIMIT :limit
"""

_UPSERT_1M_SQL = text(
    """
    INSERT INTO public.us_candles_1m (time, symbol, exchange, open, high, low, close, volume, value)
    VALUES (:time, :symbol, :exchange, :open, :high, :low, :close, :volume, :value)
    ON CONFLICT (time, symbol, exchange)
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        value = EXCLUDED.value
    WHERE
        us_candles_1m.open IS DISTINCT FROM EXCLUDED.open
        OR us_candles_1m.high IS DISTINCT FROM EXCLUDED.high
        OR us_candles_1m.low IS DISTINCT FROM EXCLUDED.low
        OR us_candles_1m.close IS DISTINCT FROM EXCLUDED.close
        OR us_candles_1m.volume IS DISTINCT FROM EXCLUDED.volume
        OR us_candles_1m.value IS DISTINCT FROM EXCLUDED.value
    """
)


def _async_session() -> AsyncSession:
    return cast(AsyncSession, cast(object, AsyncSessionLocal()))


def _empty_internal_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_INTERNAL_FRAME_COLUMNS)


def _empty_output_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_OUTPUT_FRAME_COLUMNS)


def _to_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _ensure_utc(value: dt.datetime) -> dt.datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(dt.UTC)
    else:
        timestamp = timestamp.tz_convert(dt.UTC)
    return timestamp.to_pydatetime().astimezone(dt.UTC)


def _et_naive_to_utc(value: dt.datetime) -> dt.datetime:
    if value.tzinfo is not None:
        value = value.astimezone(_ET).replace(tzinfo=None)
    return value.replace(tzinfo=_ET).astimezone(dt.UTC)


def _utc_to_et_naive(value: dt.datetime) -> dt.datetime:
    return _ensure_utc(value).astimezone(_ET).replace(tzinfo=None)


def _session_for_et_naive(value: dt.datetime) -> SessionType | None:
    time_value = value.time()
    if dt.time(4, 0, 0) <= time_value < dt.time(9, 30, 0):
        return "PRE_MARKET"
    if dt.time(9, 30, 0) <= time_value < dt.time(16, 0, 0):
        return "REGULAR"
    if dt.time(16, 0, 0) <= time_value <= dt.time(20, 0, 0):
        return "POST_MARKET"
    return None


def _hour_bucket_start_et(value: dt.datetime) -> dt.datetime | None:
    session = _session_for_et_naive(value)
    if session is None:
        return None
    current_date = value.date()
    current_time = value.time()
    if session == "PRE_MARKET":
        return dt.datetime.combine(current_date, dt.time(current_time.hour, 0, 0))
    if session == "REGULAR":
        if current_time < dt.time(10, 30, 0):
            return dt.datetime.combine(current_date, dt.time(9, 30, 0))
        return dt.datetime.combine(current_date, dt.time(current_time.hour, 30, 0))
    return dt.datetime.combine(current_date, dt.time(current_time.hour, 0, 0))


def _bucket_start_utc(value: dt.datetime, period: str) -> dt.datetime | None:
    timestamp_et = pd.Timestamp(_ensure_utc(value)).tz_convert(_ET)
    timestamp_et_naive = timestamp_et.to_pydatetime().replace(tzinfo=None)
    if period == "1m":
        if _session_for_et_naive(timestamp_et_naive) is None:
            return None
        bucket_et = timestamp_et.floor("min")
    elif period in {"5m", "15m", "30m"}:
        if _session_for_et_naive(timestamp_et_naive) is None:
            return None
        minutes = _INTRADAY_PERIOD_CONFIGS[period].bucket_minutes
        bucket_et = timestamp_et.floor(f"{minutes}min")
    elif period == "1h":
        bucket_et_naive = _hour_bucket_start_et(timestamp_et_naive)
        if bucket_et_naive is None:
            return None
        bucket_et = pd.Timestamp(bucket_et_naive, tz=_ET)
    else:
        raise ValueError(f"Unsupported US intraday period: {period}")
    bucket_et_python = bucket_et.to_pydatetime()
    return bucket_et_python.astimezone(dt.UTC)


def _internal_frame_from_rows(
    rows: list[dict[str, object]],
    datetime_key: str,
) -> pd.DataFrame:
    if not rows:
        return _empty_internal_frame()
    frame_rows: list[dict[str, object]] = []
    for row in rows:
        raw_datetime = row.get(datetime_key)
        if not isinstance(raw_datetime, dt.datetime):
            continue
        frame_rows.append(
            {
                "datetime": _ensure_utc(raw_datetime),
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
                "value": _to_float(row.get("value")),
            }
        )
    if not frame_rows:
        return _empty_internal_frame()
    return (
        pd.DataFrame(frame_rows, columns=_INTERNAL_FRAME_COLUMNS)
        .sort_values("datetime")
        .reset_index(drop=True)
    )


def _merge_internal_frames(*frames: pd.DataFrame) -> pd.DataFrame:
    available = [frame for frame in frames if not frame.empty]
    if not available:
        return _empty_internal_frame()
    merged = pd.concat(available, ignore_index=True)
    merged = merged.sort_values("datetime")
    merged = merged.drop_duplicates(subset=["datetime"], keep="last")
    return merged.reset_index(drop=True)


def _aggregate_minutes_to_period_utc(
    minutes: pd.DataFrame, period: str
) -> pd.DataFrame:
    if minutes.empty:
        return _empty_internal_frame()
    if period == "1m":
        return _merge_internal_frames(minutes)
    frame = minutes.copy()
    frame["bucket"] = frame["datetime"].apply(
        lambda value: _bucket_start_utc(value, period)
    )
    frame = frame.dropna(subset=["bucket"])
    if frame.empty:
        return _empty_internal_frame()
    aggregated = (
        frame.groupby("bucket", sort=True)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
            value=("value", "sum"),
        )
        .reset_index()
        .rename(columns={"bucket": "datetime"})
    )
    return aggregated.loc[:, _INTERNAL_FRAME_COLUMNS].reset_index(drop=True)


def _to_output_frame(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return _empty_output_frame()
    rows: list[dict[str, object]] = []
    for row in frame.sort_values("datetime").to_dict("records"):
        raw_datetime = row.get("datetime")
        if not isinstance(raw_datetime, dt.datetime):
            continue
        et_naive = _utc_to_et_naive(raw_datetime)
        session = _session_for_et_naive(et_naive)
        if session is None:
            continue
        rows.append(
            {
                "datetime": et_naive,
                "date": et_naive.date(),
                "time": et_naive.time(),
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
                "value": _to_float(row.get("value")),
                "session": session,
            }
        )
    if not rows:
        return _empty_output_frame()
    return pd.DataFrame(rows, columns=_OUTPUT_FRAME_COLUMNS)


async def _fetch_candles_1m_from_db(
    *,
    symbol: str,
    exchange: str,
    end_time_utc: dt.datetime,
    limit: int,
) -> pd.DataFrame:
    async with _async_session() as session:
        result = await session.execute(
            _US_CANDLES_1M_QUERY,
            {
                "symbol": symbol,
                "exchange": exchange,
                "end_time": end_time_utc,
                "limit": limit,
            },
        )
        rows = [
            {str(key): value for key, value in row.items()}
            for row in result.mappings().all()
        ]
    return _internal_frame_from_rows(rows, "time")


async def _fetch_candles_from_cagg(
    *,
    config: _IntradayPeriodConfig,
    symbol: str,
    exchange: str,
    end_time_utc: dt.datetime,
    limit: int,
) -> pd.DataFrame:
    if config.history_table is None:
        return _empty_internal_frame()
    query = text(_US_CANDLES_CAGG_QUERY_TEMPLATE.format(table=config.history_table))
    async with _async_session() as session:
        result = await session.execute(
            query,
            {
                "symbol": symbol,
                "exchange": exchange,
                "end_time": end_time_utc,
                "limit": limit,
            },
        )
        rows = [
            {str(key): value for key, value in row.items()}
            for row in result.mappings().all()
        ]
    return _internal_frame_from_rows(rows, "bucket")


async def _fetch_minutes_from_kis(
    *,
    symbol: str,
    exchange: str,
    end_time_et: dt.datetime,
    required_buckets: set[dt.datetime],
    required_window_bucket_count: int,
    period: str,
) -> pd.DataFrame:
    """Fetch minutes from KIS until all required buckets are covered.

    Stops paging when aggregated minutes cover all required buckets,
    or when a scaled safety cap is reached.
    """
    if not required_buckets:
        return _empty_internal_frame()

    kis = KISClient()
    all_rows: list[dict[str, object]] = []

    bucket_minutes = _INTRADAY_PERIOD_CONFIGS[period].bucket_minutes
    window_bucket_count = max(int(required_window_bucket_count), 1)
    max_pages = max(1, math.ceil(window_bucket_count * bucket_minutes / 120) + 1)

    current_keyb = end_time_et.strftime("%Y%m%d%H%M%S")
    upper_bound_utc = _et_naive_to_utc(end_time_et)
    page_calls = 0

    while page_calls < max_pages:
        page_calls += 1
        page = await kis.inquire_overseas_minute_chart(
            symbol=symbol,
            exchange_code=exchange,
            n=120,
            keyb=current_keyb,
        )
        if page.frame.empty:
            break
        page_rows: list[dict[str, object]] = []
        for item in page.frame.to_dict("records"):
            raw_datetime = item.get("datetime")
            if raw_datetime is None:
                continue
            timestamp = pd.Timestamp(raw_datetime)
            if timestamp.tzinfo is None:
                utc_datetime = _et_naive_to_utc(timestamp.to_pydatetime())
            else:
                utc_datetime = _ensure_utc(timestamp.to_pydatetime())
            if utc_datetime > upper_bound_utc:
                continue
            session = _session_for_et_naive(_utc_to_et_naive(utc_datetime))
            if session is None:
                continue
            open_value = _parse_float(item.get("open"))
            high_value = _parse_float(item.get("high"))
            low_value = _parse_float(item.get("low"))
            close_value = _parse_float(item.get("close"))
            volume_value = _parse_float(item.get("volume"))
            value_value = _parse_float(item.get("value"))
            if None in {open_value, high_value, low_value, close_value, volume_value}:
                continue
            page_rows.append(
                {
                    "datetime": utc_datetime,
                    "open": float(cast(float, open_value)),
                    "high": float(cast(float, high_value)),
                    "low": float(cast(float, low_value)),
                    "close": float(cast(float, close_value)),
                    "volume": float(cast(float, volume_value)),
                    "value": float(value_value) if value_value is not None else 0.0,
                }
            )
        all_rows.extend(page_rows)

        # Check if we have enough data to cover required buckets
        if all_rows:
            collected = _merge_internal_frames(
                pd.DataFrame(all_rows, columns=_INTERNAL_FRAME_COLUMNS)
            )
            aggregated = _aggregate_minutes_to_period_utc(collected, period)
            covered_buckets: set[dt.datetime] = set()
            for raw_datetime in aggregated["datetime"].tolist():
                if isinstance(raw_datetime, dt.datetime):
                    covered_buckets.add(_ensure_utc(raw_datetime))
            if required_buckets <= covered_buckets:
                break

        next_keyb = str(page.next_keyb or "").strip()
        if not page.has_more or not next_keyb or next_keyb == current_keyb:
            break
        current_keyb = next_keyb

    if not all_rows:
        return _empty_internal_frame()
    return _merge_internal_frames(
        pd.DataFrame(all_rows, columns=_INTERNAL_FRAME_COLUMNS)
    )


async def _self_heal_1m_candles(
    *,
    symbol: str,
    exchange: str,
    minute_rows: list[dict[str, object]],
) -> None:
    if not minute_rows:
        return
    try:
        async with _async_session() as session:
            for row in minute_rows:
                raw_datetime = row.get("datetime")
                if not isinstance(raw_datetime, dt.datetime):
                    continue
                if raw_datetime.tzinfo is None:
                    time_utc = _et_naive_to_utc(raw_datetime)
                else:
                    time_utc = _ensure_utc(raw_datetime)
                await session.execute(
                    _UPSERT_1M_SQL,
                    {
                        "time": time_utc,
                        "symbol": symbol,
                        "exchange": exchange,
                        "open": _to_float(row.get("open")),
                        "high": _to_float(row.get("high")),
                        "low": _to_float(row.get("low")),
                        "close": _to_float(row.get("close")),
                        "volume": _to_float(row.get("volume")),
                        "value": _to_float(row.get("value")),
                    },
                )
            await session.commit()
    except Exception:
        logger.exception(
            "Failed to self-heal minute candles for %s:%s", symbol, exchange
        )


def _schedule_background_self_heal(
    *,
    symbol: str,
    exchange: str,
    minute_rows: list[dict[str, object]],
) -> None:
    if not minute_rows:
        return
    task = asyncio.create_task(
        _self_heal_1m_candles(
            symbol=symbol,
            exchange=exchange,
            minute_rows=minute_rows,
        )
    )

    def _log_exception(completed: asyncio.Task[None]) -> None:
        try:
            completed.result()
        except Exception:
            logger.exception("Background self-heal task crashed")

    task.add_done_callback(_log_exception)


def _resolve_end_time_et(
    end_date: dt.datetime | None,
    end_date_is_date_only: bool,
) -> dt.datetime:
    if end_date is None:
        return dt.datetime.now(_ET).replace(tzinfo=None)
    if end_date_is_date_only:
        return dt.datetime.combine(end_date.date(), dt.time(20, 0, 0))
    if end_date.tzinfo is None:
        return end_date
    return end_date.astimezone(_ET).replace(tzinfo=None)


def _expected_recent_buckets_utc(
    *,
    period: str,
    count: int,
    end_time_utc: dt.datetime,
) -> list[dt.datetime]:
    target_count = max(int(count), 1)
    cursor = _ensure_utc(end_time_utc)
    buckets: list[dt.datetime] = []
    seen: set[dt.datetime] = set()
    max_steps = 60 * 24 * 30
    steps = 0
    while len(buckets) < target_count and steps < max_steps:
        bucket = _bucket_start_utc(cursor, period)
        if bucket is not None and bucket not in seen:
            seen.add(bucket)
            buckets.append(bucket)
        cursor -= dt.timedelta(minutes=1)
        steps += 1
    return buckets


def _get_missing_buckets(
    frame: pd.DataFrame,
    period: str,
    count: int,
    end_time_utc: dt.datetime,
) -> set[dt.datetime]:
    """Return the set of expected buckets that are missing from the frame."""
    missing_buckets, _ = _get_missing_buckets_with_repair_window(
        frame,
        period,
        count,
        end_time_utc,
    )
    return missing_buckets


def _get_missing_buckets_with_repair_window(
    frame: pd.DataFrame,
    period: str,
    count: int,
    end_time_utc: dt.datetime,
) -> tuple[set[dt.datetime], list[dt.datetime]]:
    expected_buckets = _expected_recent_buckets_utc(
        period=period,
        count=count,
        end_time_utc=end_time_utc,
    )
    if not expected_buckets:
        return set(), []

    available_buckets: set[dt.datetime] = set()
    for raw_datetime in frame["datetime"].tolist():
        if isinstance(raw_datetime, dt.datetime):
            bucket = _bucket_start_utc(raw_datetime, period)
            if bucket is not None:
                available_buckets.add(bucket)

    missing_buckets = set(expected_buckets) - available_buckets
    if not missing_buckets:
        return set(), []

    oldest_missing_index = max(
        index
        for index, bucket in enumerate(expected_buckets)
        if bucket in missing_buckets
    )
    return missing_buckets, expected_buckets[: oldest_missing_index + 1]


async def read_us_intraday_candles(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: dt.datetime | None = None,
    end_date_is_date_only: bool = False,
) -> pd.DataFrame:
    normalized_period = str(period or "1h").strip().lower()
    if normalized_period not in _INTRADAY_PERIOD_CONFIGS:
        raise ValueError(f"Unsupported US intraday period: {period}")
    capped_count = max(int(count), 1)
    try:
        exchange = await get_us_exchange_by_symbol(symbol)
    except USSymbolUniverseLookupError:
        raise
    end_time_et = _resolve_end_time_et(end_date, end_date_is_date_only)
    end_time_utc = _et_naive_to_utc(end_time_et)

    if normalized_period == "1h":
        db_minutes = await _fetch_candles_1m_from_db(
            symbol=symbol,
            exchange=exchange,
            end_time_utc=end_time_utc,
            limit=max(capped_count * 120, capped_count),
        )
        merged_minutes = db_minutes
        output_internal = _aggregate_minutes_to_period_utc(db_minutes, "1h")
        missing_buckets, repair_window = _get_missing_buckets_with_repair_window(
            output_internal,
            "1h",
            capped_count,
            end_time_utc,
        )
        if missing_buckets:
            fallback_minutes = await _fetch_minutes_from_kis(
                symbol=symbol,
                exchange=exchange,
                end_time_et=end_time_et,
                required_buckets=missing_buckets,
                required_window_bucket_count=len(repair_window),
                period="1h",
            )
            if not fallback_minutes.empty:
                merged_minutes = _merge_internal_frames(db_minutes, fallback_minutes)
                _schedule_background_self_heal(
                    symbol=symbol,
                    exchange=exchange,
                    minute_rows=cast(
                        list[dict[str, object]], fallback_minutes.to_dict("records")
                    ),
                )
                output_internal = _aggregate_minutes_to_period_utc(merged_minutes, "1h")
        return _to_output_frame(output_internal.tail(capped_count)).reset_index(
            drop=True
        )

    config = _INTRADAY_PERIOD_CONFIGS[normalized_period]
    if normalized_period == "1m":
        output_internal = await _fetch_candles_1m_from_db(
            symbol=symbol,
            exchange=exchange,
            end_time_utc=end_time_utc,
            limit=max(capped_count * 2, capped_count),
        )
        missing_buckets, repair_window = _get_missing_buckets_with_repair_window(
            output_internal,
            "1m",
            capped_count,
            end_time_utc,
        )
        if missing_buckets:
            fallback_minutes = await _fetch_minutes_from_kis(
                symbol=symbol,
                exchange=exchange,
                end_time_et=end_time_et,
                required_buckets=missing_buckets,
                required_window_bucket_count=len(repair_window),
                period="1m",
            )
            if not fallback_minutes.empty:
                output_internal = _merge_internal_frames(
                    output_internal, fallback_minutes
                )
                _schedule_background_self_heal(
                    symbol=symbol,
                    exchange=exchange,
                    minute_rows=cast(
                        list[dict[str, object]], fallback_minutes.to_dict("records")
                    ),
                )
        return _to_output_frame(output_internal.tail(capped_count)).reset_index(
            drop=True
        )

    output_internal = await _fetch_candles_from_cagg(
        config=config,
        symbol=symbol,
        exchange=exchange,
        end_time_utc=end_time_utc,
        limit=max(capped_count * 2, capped_count),
    )
    missing_buckets, repair_window = _get_missing_buckets_with_repair_window(
        output_internal,
        normalized_period,
        capped_count,
        end_time_utc,
    )
    if missing_buckets:
        fallback_minutes = await _fetch_minutes_from_kis(
            symbol=symbol,
            exchange=exchange,
            end_time_et=end_time_et,
            required_buckets=missing_buckets,
            required_window_bucket_count=len(repair_window),
            period=normalized_period,
        )
        if not fallback_minutes.empty:
            fallback_aggregated = _aggregate_minutes_to_period_utc(
                fallback_minutes,
                normalized_period,
            )
            output_internal = _merge_internal_frames(
                output_internal, fallback_aggregated
            )
            _schedule_background_self_heal(
                symbol=symbol,
                exchange=exchange,
                minute_rows=cast(
                    list[dict[str, object]], fallback_minutes.to_dict("records")
                ),
            )
    return _to_output_frame(output_internal.tail(capped_count)).reset_index(drop=True)


__all__ = ["read_us_intraday_candles"]
