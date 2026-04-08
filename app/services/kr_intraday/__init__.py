"""KR intraday candles package — public API and orchestration functions."""

from __future__ import annotations

import datetime
from typing import Any

import pandas as pd

from app.services.kr_intraday._kis_api import (
    _fetch_historical_minutes_via_kis,
    _load_recent_overlay_frame,
    _normalize_intraday_rows,
)
from app.services.kr_intraday._repository import (
    _fetch_intraday_history_rows,
    _resolve_universe_row,
    _schedule_background_minute_storage,
    _store_minute_candles_background,
    _UniverseError,
)
from app.services.kr_intraday._types import (
    _INTRADAY_FRAME_COLUMNS,
    _INTRADAY_PERIOD_CONFIGS,
    _KST,
    _VENUE_CONFIGS,
)
from app.services.kr_intraday._utils import (
    _aggregate_minutes_to_buckets,
    _empty_intraday_frame,
    _ensure_kst_aware,
    _history_rows_to_frame,
    _merge_minute_rows,
    _merge_overlay_into_intraday_frame,
    _to_kst_naive_series,
)

__all__ = [
    "read_kr_intraday_candles",
    "read_kr_hourly_candles_1h",
    "_aggregate_minutes_to_hourly",
    "_empty_intraday_frame",
    "_INTRADAY_FRAME_COLUMNS",
    "_INTRADAY_PERIOD_CONFIGS",
    "_normalize_intraday_rows",
    "_schedule_background_minute_storage",
    "_store_minute_candles_background",
    "_VENUE_CONFIGS",
]


def _aggregate_minutes_to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    """Aggregate minute candles to hourly candles."""
    aggregated = _aggregate_minutes_to_buckets(df, bucket_minutes=60)
    if aggregated.empty:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
    return aggregated[["datetime", "open", "high", "low", "close", "volume"]]


async def read_kr_intraday_candles(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: datetime.datetime | None,
    now_kst: datetime.datetime | None = None,
) -> pd.DataFrame:
    """Read Korean stock intraday candles for the specified period."""
    normalized_period = str(period or "1h").strip().lower()
    if normalized_period == "1h":
        return await read_kr_hourly_candles_1h(
            symbol=symbol,
            count=count,
            end_date=end_date,
            now_kst=now_kst,
        )

    config = _INTRADAY_PERIOD_CONFIGS.get(normalized_period)
    if config is None:
        raise ValueError(f"Unsupported KR intraday period: {period}")

    capped_count = max(int(count), 1)
    resolved_now = _ensure_kst_aware(now_kst or datetime.datetime.now(_KST))
    universe = await _resolve_universe_row(symbol)
    if isinstance(universe, _UniverseError):
        return _empty_intraday_frame()

    end_day, end_time_kst = _resolve_intraday_end_bounds(
        resolved_now=resolved_now,
        end_date=end_date,
    )

    history_rows = await _fetch_intraday_history_rows(
        config=config,
        symbol=universe.symbol,
        end_time_kst=end_time_kst,
        limit=min(max(capped_count * 3, capped_count + 12), 1000),
    )
    out = _history_rows_to_frame(config=config, rows=history_rows)

    if end_day == resolved_now.date():
        overlay_start = max(
            datetime.datetime.combine(end_day, datetime.time(8, 0, 0), tzinfo=_KST),
            resolved_now - datetime.timedelta(minutes=30),
        )
        overlay_frame, overlay_api_minute_rows = await _load_recent_overlay_frame(
            symbol=universe.symbol,
            start_time_kst=overlay_start,
            end_time_kst=resolved_now + datetime.timedelta(minutes=1),
            now_kst=resolved_now,
            nxt_eligible=universe.nxt_eligible,
            end_date=end_date,
        )
        out = _merge_overlay_into_intraday_frame(
            out=out,
            overlay_frame=overlay_frame,
            bucket_minutes=config.bucket_minutes,
        )
    else:
        overlay_api_minute_rows = []

    fallback_api_minute_rows: list[Any] = []
    if end_day == resolved_now.date() and len(out) < capped_count:
        _, fallback_api_minute_rows = await _fetch_historical_minutes_via_kis(
            symbol=universe.symbol,
            end_date=pd.Timestamp(end_day).date(),
            limit=capped_count,
        )
        if fallback_api_minute_rows:
            out = _merge_overlay_into_intraday_frame(
                out=out,
                overlay_frame=_merge_minute_rows(fallback_api_minute_rows),
                bucket_minutes=config.bucket_minutes,
            )

    all_api_minute_rows = list(overlay_api_minute_rows)
    if fallback_api_minute_rows:
        all_api_minute_rows.extend(fallback_api_minute_rows)
    _schedule_background_minute_storage(
        symbol=universe.symbol,
        minute_rows=all_api_minute_rows,
    )

    if out.empty:
        return _empty_intraday_frame()

    out = out.sort_values("datetime").reset_index(drop=True)
    out["datetime"] = _to_kst_naive_series(out["datetime"])
    return out.tail(capped_count).reset_index(drop=True)


async def read_kr_hourly_candles_1h(
    *,
    symbol: str,
    count: int,
    end_date: datetime.datetime | None,
    now_kst: datetime.datetime | None = None,
) -> pd.DataFrame:
    """
    Read Korean stock hourly candles with DB-first query and KIS API fallback.

    Implements graceful degradation: returns partial or empty data instead of raising.
    """
    capped_count = max(int(count), 1)
    resolved_now = _ensure_kst_aware(now_kst or datetime.datetime.now(_KST))

    universe = await _resolve_universe_row(symbol)
    if isinstance(universe, _UniverseError):
        return pd.DataFrame(
            columns=[
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
                "venues",
            ]
        )

    if end_date is None:
        end_time_kst = resolved_now
    else:
        end_day = (
            _ensure_kst_aware(end_date).date() if end_date.tzinfo else end_date.date()
        )
        end_time_kst = datetime.datetime.combine(
            end_day, datetime.time(20, 0, 0), tzinfo=_KST
        )

    from app.services.kr_intraday._repository import _fetch_hour_rows

    hour_rows = await _fetch_hour_rows(
        symbol=universe.symbol,
        end_time_kst=end_time_kst,
        limit=min(max(capped_count * 3, capped_count + 12), 1000),
    )

    out = _history_rows_to_frame(
        config=_INTRADAY_PERIOD_CONFIGS["1h"],
        rows=hour_rows,
    )

    if end_time_kst.date() == resolved_now.date():
        overlay_start = max(
            datetime.datetime.combine(
                resolved_now.date(), datetime.time(8, 0, 0), tzinfo=_KST
            ),
            resolved_now - datetime.timedelta(minutes=30),
        )
        overlay_frame, overlay_api_minute_rows = await _load_recent_overlay_frame(
            symbol=universe.symbol,
            start_time_kst=overlay_start,
            end_time_kst=resolved_now + datetime.timedelta(minutes=1),
            now_kst=resolved_now,
            nxt_eligible=universe.nxt_eligible,
            end_date=end_date,
        )
        out = _merge_overlay_into_intraday_frame(
            out=out,
            overlay_frame=overlay_frame,
            bucket_minutes=60,
        )
    else:
        overlay_api_minute_rows = []

    fallback_api_minute_rows: list[Any] = []
    if end_time_kst.date() == resolved_now.date() and len(out) < capped_count:
        _, fallback_api_minute_rows = await _fetch_historical_minutes_via_kis(
            symbol=universe.symbol,
            end_date=pd.Timestamp(end_time_kst.date()).date(),
            limit=capped_count,
        )
        if fallback_api_minute_rows:
            out = _merge_overlay_into_intraday_frame(
                out=out,
                overlay_frame=_merge_minute_rows(fallback_api_minute_rows),
                bucket_minutes=60,
            )

    all_api_minute_rows = list(overlay_api_minute_rows)
    if fallback_api_minute_rows:
        all_api_minute_rows.extend(fallback_api_minute_rows)
    _schedule_background_minute_storage(
        symbol=universe.symbol,
        minute_rows=all_api_minute_rows,
    )

    if out.empty:
        return _empty_intraday_frame()

    out = out.sort_values("datetime").reset_index(drop=True)
    out["datetime"] = _to_kst_naive_series(out["datetime"])
    return out.tail(capped_count).reset_index(drop=True)


def _resolve_intraday_end_bounds(
    *,
    resolved_now: datetime.datetime,
    end_date: datetime.datetime | None,
) -> tuple[datetime.date, datetime.datetime]:
    if end_date is None:
        return resolved_now.date(), resolved_now

    end_day = _ensure_kst_aware(end_date).date() if end_date.tzinfo else end_date.date()
    return end_day, datetime.datetime.combine(
        end_day,
        datetime.time(20, 0, 0),
        tzinfo=_KST,
    )
