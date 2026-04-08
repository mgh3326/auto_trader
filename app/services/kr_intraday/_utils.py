from __future__ import annotations

import datetime
from typing import Any, cast

import pandas as pd

from app.services.kr_intraday._types import (
    SessionType,
    VenueType,
    _INTRADAY_FRAME_COLUMNS,
    _KST,
    _MinuteRow,
    logger,
)


def _ensure_kst_aware(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=_KST)
    return value.astimezone(_KST)


def _convert_kis_datetime_to_utc(kst_dt: datetime.datetime) -> datetime.datetime:
    """Convert KIS API datetime (KST) to UTC for storage."""
    kst_aware = _ensure_kst_aware(kst_dt)
    return kst_aware.astimezone(datetime.UTC).replace(tzinfo=None)


def _to_kst_naive(value: datetime.datetime) -> datetime.datetime:
    return _ensure_kst_aware(value).replace(tzinfo=None)


def _to_kst_naive_series(values: pd.Series) -> pd.Series:
    return values.map(
        lambda value: (
            pd.NaT
            if pd.isna(value)
            else pd.Timestamp(_to_kst_naive(pd.Timestamp(value).to_pydatetime()))
        )
    )


def _to_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


def _parse_float(value: object) -> float | None:
    """Parse a value to float, returning None for invalid values."""
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _to_venue(value: object) -> VenueType | None:
    """
    Convert value to VenueType (KRX or NTX).

    Returns None for invalid venues instead of raising ValueError,
    implementing graceful degradation.
    """
    text_value = str(value or "").strip().upper()
    if text_value == "KRX":
        return "KRX"
    if text_value == "NTX":
        return "NTX"
    # Log warning but return None instead of raising ValueError
    logger.warning("Unexpected KR venue: %s, returning None", value)
    return None


def _normalize_venues(value: object) -> list[str]:
    venues: list[str] = []
    if value is None:
        return venues
    if isinstance(value, (list, tuple)):
        venues = [str(v).strip().upper() for v in value if str(v).strip()]
    else:
        venues = [str(value).strip().upper()]
    order = {"KRX": 0, "NTX": 1}
    venues = [v for v in venues if v in order]
    venues.sort(key=lambda v: order[v])
    return venues


def _session_for_bucket_start(
    bucket_start_kst_naive: datetime.datetime,
) -> SessionType | None:
    t = bucket_start_kst_naive.time()
    if datetime.time(8, 0, 0) <= t < datetime.time(9, 0, 0):
        return "PRE_MARKET"
    if datetime.time(9, 0, 0) <= t < datetime.time(15, 30, 0):
        return "REGULAR"
    if datetime.time(15, 30, 0) <= t <= datetime.time(20, 0, 0):
        return "AFTER_MARKET"
    return None


def _aggregate_minutes_to_hourly(df: pd.DataFrame) -> pd.DataFrame:
    aggregated = _aggregate_minutes_to_buckets(df, bucket_minutes=60)
    if aggregated.empty:
        return pd.DataFrame(
            columns=["datetime", "open", "high", "low", "close", "volume"]
        )
    return aggregated[["datetime", "open", "high", "low", "close", "volume"]]


def _empty_intraday_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_INTRADAY_FRAME_COLUMNS)


def _dedupe_normalized_venues(values: list[object]) -> list[str]:
    venues: list[str] = []
    for value in values:
        venues.extend(_normalize_venues(value))
    return list(dict.fromkeys(_normalize_venues(venues)))


def _prepare_bucket_aggregation_frame(df: pd.DataFrame) -> pd.DataFrame:
    required_cols = {"datetime", "open", "high", "low", "close", "volume"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        logger.warning(
            "Missing required columns for aggregation: %s",
            sorted(missing_cols),
        )
        return _empty_intraday_frame()

    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])
    if out.empty:
        return _empty_intraday_frame()

    if "value" not in out.columns:
        out["value"] = 0.0
    if "venues" not in out.columns:
        if "venue" in out.columns:
            out["venues"] = out["venue"].apply(_normalize_venues)
        else:
            out["venues"] = [[] for _ in range(len(out))]
    else:
        out["venues"] = out["venues"].apply(_normalize_venues)
    return out


def _build_bucket_frame_row(
    bucket_dt: datetime.datetime, group: pd.DataFrame
) -> dict[str, object] | None:
    session = _session_for_bucket_start(bucket_dt)
    if session is None:
        return None

    return {
        "datetime": bucket_dt,
        "date": bucket_dt.date(),
        "time": bucket_dt.time(),
        "open": float(group["open"].iloc[0]),
        "high": float(group["high"].max()),
        "low": float(group["low"].min()),
        "close": float(group["close"].iloc[-1]),
        "volume": float(group["volume"].sum()),
        "value": float(group["value"].sum()),
        "session": session,
        "venues": _dedupe_normalized_venues(group["venues"].tolist()),
    }


def _aggregate_minutes_to_buckets(
    df: pd.DataFrame,
    *,
    bucket_minutes: int,
) -> pd.DataFrame:
    if df.empty:
        return _empty_intraday_frame()

    out = _prepare_bucket_aggregation_frame(df)
    if out.empty:
        return _empty_intraday_frame()

    bucket_label = f"{bucket_minutes}min"
    out["bucket"] = out["datetime"].dt.floor(bucket_label)

    rows: list[dict[str, object]] = []
    for bucket_value, group in out.groupby("bucket", sort=True):
        try:
            bucket_dt = pd.Timestamp(cast(Any, bucket_value)).to_pydatetime()
        except Exception:
            continue
        row = _build_bucket_frame_row(bucket_dt, group)
        if row is not None:
            rows.append(row)

    if not rows:
        return _empty_intraday_frame()

    return pd.DataFrame(rows, columns=_INTRADAY_FRAME_COLUMNS)


def _merge_minute_rows(rows: list[_MinuteRow]) -> pd.DataFrame:
    if not rows:
        return _empty_intraday_frame()

    minutes_by_time: dict[datetime.datetime, dict[VenueType, _MinuteRow]] = {}
    for row in rows:
        minutes_by_time.setdefault(row.minute_time, {})[row.venue] = row

    merged_rows: list[dict[str, object]] = []
    for minute_time in sorted(minutes_by_time):
        venue_rows = minutes_by_time[minute_time]
        source = venue_rows.get("KRX") or venue_rows.get("NTX")
        if source is None:
            continue
        session = _session_for_bucket_start(minute_time)
        if session is None:
            continue

        merged_rows.append(
            {
                "datetime": minute_time,
                "date": minute_time.date(),
                "time": minute_time.time(),
                "open": float(source.open),
                "high": float(source.high),
                "low": float(source.low),
                "close": float(source.close),
                "volume": float(sum(item.volume for item in venue_rows.values())),
                "value": float(sum(item.value for item in venue_rows.values())),
                "session": session,
                "venues": _normalize_venues(list(venue_rows.keys())),
            }
        )

    if not merged_rows:
        return _empty_intraday_frame()

    return pd.DataFrame(merged_rows, columns=_INTRADAY_FRAME_COLUMNS)


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


def _resolve_window_minute_time(value: object) -> datetime.datetime | None:
    if isinstance(value, datetime.datetime):
        return _to_kst_naive(value).replace(second=0, microsecond=0)
    elif isinstance(value, str | datetime.date):
        parsed = pd.to_datetime(value, errors="coerce")
    else:
        return None
    if pd.isna(parsed):
        return None
    return _to_kst_naive(pd.Timestamp(parsed).to_pydatetime()).replace(
        second=0,
        microsecond=0,
    )


def _minute_row_from_source(
    *, minute_time: datetime.datetime, venue: VenueType, source: object
) -> _MinuteRow:
    item = cast(Any, source)
    return _MinuteRow(
        minute_time=minute_time,
        venue=venue,
        open=_to_float(item.get("open")),
        high=_to_float(item.get("high")),
        low=_to_float(item.get("low")),
        close=_to_float(item.get("close")),
        volume=_to_float(item.get("volume")),
        value=_to_float(item.get("value")),
    )


def _store_minute_row(
    minute_by_key: dict[tuple[datetime.datetime, VenueType], _MinuteRow],
    *,
    minute_time: datetime.datetime,
    venue: VenueType,
    source: object,
    api_minute_rows: list[_MinuteRow] | None = None,
) -> None:
    minute_row = _minute_row_from_source(
        minute_time=minute_time,
        venue=venue,
        source=source,
    )
    minute_by_key[(minute_time, venue)] = minute_row
    if api_minute_rows is not None:
        api_minute_rows.append(minute_row)
