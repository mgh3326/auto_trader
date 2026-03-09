from __future__ import annotations

import asyncio
import datetime
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, time, timedelta
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient

_KST = ZoneInfo("Asia/Seoul")

_MAX_PAGE_CALLS_PER_DAY = 30

_KR_UNIVERSE_SYNC_COMMAND = "uv run python scripts/sync_kr_symbol_universe.py"

logger = logging.getLogger(__name__)


def _kr_universe_sync_hint() -> str:
    return f"Sync required: {_KR_UNIVERSE_SYNC_COMMAND}"


SessionType = Literal["PRE_MARKET", "REGULAR", "AFTER_MARKET"]
VenueType = Literal["KRX", "NTX"]


def _async_session() -> AsyncSession:
    return cast(AsyncSession, cast(object, AsyncSessionLocal()))


@dataclass(frozen=True, slots=True)
class _UniverseRow:
    symbol: str
    nxt_eligible: bool
    is_active: bool


@dataclass(frozen=True, slots=True)
class _UniverseError:
    """Represents an error during universe lookup without raising an exception."""

    reason: str


@dataclass(frozen=True, slots=True)
class _MinuteRow:
    minute_time: datetime.datetime
    venue: VenueType
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


@dataclass(frozen=True, slots=True)
class _VenueConfig:
    """Venue-specific configuration for KIS API calls."""

    venue: VenueType
    market_code: str
    session_start: time
    session_end: time


@dataclass(frozen=True, slots=True)
class _IntradayPeriodConfig:
    period: str
    bucket_minutes: int
    history_table: str


_VENUE_CONFIGS: dict[VenueType, _VenueConfig] = {
    "KRX": _VenueConfig(
        venue="KRX",
        market_code="J",
        session_start=time(9, 0, 0),
        session_end=time(15, 30, 0),
    ),
    "NTX": _VenueConfig(
        venue="NTX",
        market_code="NX",
        session_start=time(8, 0, 0),
        session_end=time(20, 0, 0),
    ),
}

_INTRADAY_PERIOD_CONFIGS: dict[str, _IntradayPeriodConfig] = {
    "1m": _IntradayPeriodConfig(
        period="1m",
        bucket_minutes=1,
        history_table="public.kr_candles_1m",
    ),
    "5m": _IntradayPeriodConfig(
        period="5m",
        bucket_minutes=5,
        history_table="public.kr_candles_5m",
    ),
    "15m": _IntradayPeriodConfig(
        period="15m",
        bucket_minutes=15,
        history_table="public.kr_candles_15m",
    ),
    "30m": _IntradayPeriodConfig(
        period="30m",
        bucket_minutes=30,
        history_table="public.kr_candles_30m",
    ),
    "1h": _IntradayPeriodConfig(
        period="1h",
        bucket_minutes=60,
        history_table="public.kr_candles_1h",
    ),
}

_INTRADAY_FRAME_COLUMNS = [
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


_KR_UNIVERSE_HAS_ANY_ROWS_SQL = text(
    """
    SELECT symbol
    FROM public.kr_symbol_universe
    LIMIT 1
    """
)

_KR_UNIVERSE_ROW_SQL = text(
    """
    SELECT symbol, nxt_eligible, is_active
    FROM public.kr_symbol_universe
    WHERE symbol = :symbol
    """
)

_KR_HOURLY_SQL = text(
    """
    SELECT bucket, open, high, low, close, volume, value, venues
    FROM public.kr_candles_1h
    WHERE symbol = :symbol
      AND bucket <= :end_time
    ORDER BY bucket DESC
    LIMIT :limit
    """
)

_KR_MINUTE_SQL = text(
    """
    SELECT time, venue, open, high, low, close, volume, value
    FROM public.kr_candles_1m
    WHERE symbol = :symbol
      AND time >= :start_time
      AND time < :end_time
    """
)

_KR_MINUTE_HISTORY_SQL = text(
    """
    SELECT time, venue, open, high, low, close, volume, value
    FROM public.kr_candles_1m
    WHERE symbol = :symbol
      AND time <= :end_time
    ORDER BY time DESC
    LIMIT :limit
    """
)

_UPSERT_SQL = text(
    """
    INSERT INTO public.kr_candles_1m (symbol, time, venue, open, high, low, close, volume, value)
    VALUES (:symbol, :time, :venue, :open, :high, :low, :close, :volume, :value)
    ON CONFLICT (time, symbol, venue)
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        value = EXCLUDED.value
    """
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


def _aggregate_minutes_to_buckets(
    df: pd.DataFrame,
    *,
    bucket_minutes: int,
) -> pd.DataFrame:
    if df.empty:
        return _empty_intraday_frame()

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

    bucket_label = f"{bucket_minutes}min"
    out["bucket"] = out["datetime"].dt.floor(bucket_label)

    rows: list[dict[str, object]] = []
    for bucket_value, group in out.groupby("bucket", sort=True):
        try:
            bucket_dt = pd.Timestamp(cast(Any, bucket_value)).to_pydatetime()
        except Exception:
            continue
        session = _session_for_bucket_start(bucket_dt)
        if session is None:
            continue

        venues: list[str] = []
        for value in group["venues"].tolist():
            venues.extend(_normalize_venues(value))
        normalized_venues = list(dict.fromkeys(_normalize_venues(venues)))

        rows.append(
            {
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
                "venues": normalized_venues,
            }
        )

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


def _should_call_api(
    *, now_kst: datetime.datetime, end_date: datetime.datetime | None
) -> bool:
    if end_date is not None:
        end_day = (
            _ensure_kst_aware(end_date).date() if end_date.tzinfo else end_date.date()
        )
        if end_day < now_kst.date():
            return False

    now_clock = now_kst.time()
    if now_clock < datetime.time(8, 0, 0):
        return False
    if now_clock >= datetime.time(20, 0, 0):
        return False
    return True


def _api_markets_for_now(
    *,
    now_kst: datetime.datetime,
    nxt_eligible: bool,
    end_date: datetime.datetime | None,
) -> list[str]:
    if not _should_call_api(now_kst=now_kst, end_date=end_date):
        return []

    now_clock = now_kst.time()
    if datetime.time(8, 0, 0) <= now_clock < datetime.time(9, 0, 0):
        return ["NX"] if nxt_eligible else []
    if datetime.time(9, 0, 0) <= now_clock < datetime.time(15, 35, 0):
        return ["J", "NX"] if nxt_eligible else ["J"]
    if datetime.time(15, 35, 0) <= now_clock < datetime.time(20, 0, 0):
        return ["NX"] if nxt_eligible else []
    return []


async def _resolve_universe_row(
    symbol: str,
) -> _UniverseRow | _UniverseError:
    """
    Resolve symbol from kr_symbol_universe table.

    Returns _UniverseRow if found and active, _UniverseError otherwise.
    Never raises ValueError - uses graceful degradation instead.
    """
    normalized_symbol = str(symbol or "").strip().upper()
    async with _async_session() as session:
        has_any_rows = (
            await session.execute(_KR_UNIVERSE_HAS_ANY_ROWS_SQL)
        ).scalar_one_or_none()
        result = await session.execute(
            _KR_UNIVERSE_ROW_SQL,
            {"symbol": normalized_symbol},
        )
        rows = list(result.mappings().all())

    if not rows:
        if has_any_rows is None:
            logger.warning(
                "kr_symbol_universe is empty. %s",
                _kr_universe_sync_hint(),
            )
            return _UniverseError(
                reason=f"kr_symbol_universe is empty. {_kr_universe_sync_hint()}"
            )
        logger.warning(
            "KR symbol '%s' is not registered in kr_symbol_universe. %s",
            normalized_symbol,
            _kr_universe_sync_hint(),
        )
        return _UniverseError(
            reason=f"KR symbol '{normalized_symbol}' is not registered in kr_symbol_universe. "
            f"{_kr_universe_sync_hint()}"
        )

    row = rows[0]
    is_active = bool(row.get("is_active"))
    if not is_active:
        logger.warning(
            "KR symbol '%s' is inactive in kr_symbol_universe. %s",
            normalized_symbol,
            _kr_universe_sync_hint(),
        )
        return _UniverseError(
            reason=f"KR symbol '{normalized_symbol}' is inactive in kr_symbol_universe. "
            f"{_kr_universe_sync_hint()}"
        )

    return _UniverseRow(
        symbol=normalized_symbol,
        nxt_eligible=bool(row.get("nxt_eligible")),
        is_active=is_active,
    )


async def _fetch_hour_rows(
    *,
    symbol: str,
    end_time_kst: datetime.datetime,
    limit: int,
) -> list[dict[str, object]]:
    async with _async_session() as session:
        result = await session.execute(
            _KR_HOURLY_SQL,
            {
                "symbol": symbol,
                "end_time": end_time_kst,
                "limit": int(limit),
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


async def _fetch_minute_rows(
    *,
    symbol: str,
    start_time_kst: datetime.datetime,
    end_time_kst: datetime.datetime,
) -> list[dict[str, object]]:
    async with _async_session() as session:
        result = await session.execute(
            _KR_MINUTE_SQL,
            {
                "symbol": symbol,
                "start_time": start_time_kst,
                "end_time": end_time_kst,
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


def _build_hour_frame(
    *,
    hour_rows: list[dict[str, object]],
    current_hour_row: dict[str, object] | None,
    count: int,
    current_bucket_start: datetime.datetime | None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    drop_bucket = current_bucket_start

    for row in hour_rows:
        bucket_raw = row.get("bucket")
        if not isinstance(bucket_raw, datetime.datetime):
            continue
        bucket_naive = _to_kst_naive(bucket_raw)
        if drop_bucket is not None and bucket_naive == drop_bucket:
            continue

        session = _session_for_bucket_start(bucket_naive)
        if session is None:
            continue

        venues = _normalize_venues(row.get("venues"))
        rows.append(
            {
                "datetime": bucket_naive,
                "date": bucket_naive.date(),
                "time": bucket_naive.time(),
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
                "value": _to_float(row.get("value")),
                "session": session,
                "venues": venues,
            }
        )

    if current_hour_row is not None:
        bucket_raw = current_hour_row.get("datetime")
        if isinstance(bucket_raw, datetime.datetime):
            session = _session_for_bucket_start(bucket_raw)
            if session is not None:
                current = dict(current_hour_row)
                current["session"] = session
                current["date"] = bucket_raw.date()
                current["time"] = bucket_raw.time()
                current["venues"] = _normalize_venues(current_hour_row.get("venues"))
                rows.append(current)

    if not rows:
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

    out = pd.DataFrame(rows)
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    out = out.tail(max(int(count), 1)).reset_index(drop=True)
    return out


async def _build_current_hour_row(
    *,
    symbol: str,
    now_kst: datetime.datetime,
    nxt_eligible: bool,
    end_date: datetime.datetime | None,
) -> tuple[dict[str, object] | None, datetime.datetime | None, list[_MinuteRow]]:
    current_bucket_start_kst = now_kst.replace(minute=0, second=0, microsecond=0)
    current_bucket_naive = current_bucket_start_kst.replace(tzinfo=None)

    if _session_for_bucket_start(current_bucket_naive) is None:
        return None, None, []

    if end_date is not None:
        end_day = (
            _ensure_kst_aware(end_date).date() if end_date.tzinfo else end_date.date()
        )
        if end_day != now_kst.date():
            return None, None, []

    start_time_kst = current_bucket_start_kst
    end_time_kst = start_time_kst + datetime.timedelta(hours=1)
    db_minutes = await _fetch_minute_rows(
        symbol=symbol,
        start_time_kst=start_time_kst,
        end_time_kst=end_time_kst,
    )

    minute_by_key: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}
    for row in db_minutes:
        time_raw = row.get("time")
        venue_raw = row.get("venue")
        if not isinstance(time_raw, datetime.datetime):
            continue
        venue_opt = _to_venue(venue_raw)
        # Skip rows with invalid venue (graceful degradation)
        if venue_opt is None:
            continue
        venue = cast(VenueType, venue_opt)
        minute_time = _to_kst_naive(time_raw).replace(second=0, microsecond=0)
        if not (
            current_bucket_naive
            <= minute_time
            < current_bucket_naive + datetime.timedelta(hours=1)
        ):
            continue
        minute_by_key[(minute_time, venue)] = _MinuteRow(
            minute_time=minute_time,
            venue=venue,
            open=_to_float(row.get("open")),
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=_to_float(row.get("close")),
            volume=_to_float(row.get("volume")),
            value=_to_float(row.get("value")),
        )

    markets = _api_markets_for_now(
        now_kst=now_kst,
        nxt_eligible=nxt_eligible,
        end_date=end_date,
    )

    api_minute_candles_for_db: list[_MinuteRow] = []

    if markets:
        kis = KISClient()
        api_date = pd.Timestamp(now_kst.date())
        legacy_end_time = now_kst.strftime("%H%M%S")

        async def _fetch_one(market: str) -> object:
            minute_chart = cast(Any, getattr(kis, "inquire_minute_chart", None))
            if callable(minute_chart):
                minute_chart_async = cast(
                    Callable[..., Awaitable[pd.DataFrame]],
                    minute_chart,
                )
                return await minute_chart_async(
                    code=symbol,
                    market=market,
                    time_unit=1,
                    n=30,
                    end_date=api_date,
                )
            # Support legacy KIS test doubles that only expose the historical intraday method.
            return await kis.inquire_time_dailychartprice(
                code=symbol,
                market=market,
                n=30,
                end_date=api_date,
                end_time=legacy_end_time,
            )

        frames = await asyncio.gather(
            *[_fetch_one(m) for m in markets], return_exceptions=True
        )
        for market, frame in zip(markets, frames, strict=False):
            if isinstance(frame, Exception):
                logger.warning(
                    "Current-hour KIS API call failed for %s %s: %s",
                    symbol,
                    market,
                    frame,
                )
                continue
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            venue: VenueType = "KRX" if market == "J" else "NTX"
            if "datetime" not in frame.columns:
                continue
            dt_series = pd.to_datetime(frame["datetime"], errors="coerce")
            for pos, dt_val in enumerate(dt_series.tolist()):
                if pd.isna(dt_val):
                    continue
                minute_time = (
                    pd.Timestamp(dt_val)
                    .to_pydatetime()
                    .replace(second=0, microsecond=0)
                )
                if not (
                    current_bucket_naive
                    <= minute_time
                    < current_bucket_naive + datetime.timedelta(hours=1)
                ):
                    continue
                src = frame.iloc[pos]
                minute_by_key[(minute_time, venue)] = _MinuteRow(
                    minute_time=minute_time,
                    venue=venue,
                    open=_to_float(src.get("open")),
                    high=_to_float(src.get("high")),
                    low=_to_float(src.get("low")),
                    close=_to_float(src.get("close")),
                    volume=_to_float(src.get("volume")),
                    value=_to_float(src.get("value")),
                )
                # Track minute candles for background DB storage
                api_minute_candles_for_db.append(
                    _MinuteRow(
                        minute_time=minute_time,
                        venue=venue,
                        open=_to_float(src.get("open")),
                        high=_to_float(src.get("high")),
                        low=_to_float(src.get("low")),
                        close=_to_float(src.get("close")),
                        volume=_to_float(src.get("volume")),
                        value=_to_float(src.get("value")),
                    )
                )

    if not minute_by_key:
        return None, current_bucket_naive, api_minute_candles_for_db

    minutes_by_time: dict[datetime.datetime, dict[VenueType, _MinuteRow]] = {}
    venues_seen: set[str] = set()
    for (minute_time, venue), row in minute_by_key.items():
        venues_seen.add(venue)
        minutes_by_time.setdefault(minute_time, {})[venue] = row

    combined: list[_MinuteRow] = []
    for minute_time in sorted(minutes_by_time):
        group = minutes_by_time[minute_time]
        source = group.get("KRX") or group.get("NTX")
        if source is None:
            continue
        volume = sum(r.volume for r in group.values())
        value = sum(r.value for r in group.values())
        combined.append(
            _MinuteRow(
                minute_time=minute_time,
                venue=source.venue,
                open=source.open,
                high=source.high,
                low=source.low,
                close=source.close,
                volume=volume,
                value=value,
            )
        )

    if not combined:
        return None, current_bucket_naive, api_minute_candles_for_db

    open_ = combined[0].open
    high_ = max(m.high for m in combined)
    low_ = min(m.low for m in combined)
    close_ = combined[-1].close
    volume_ = sum(m.volume for m in combined)
    value_ = sum(m.value for m in combined)
    venues = _normalize_venues(
        sorted(venues_seen, key=lambda v: 0 if v == "KRX" else 1)
    )

    return (
        {
            "datetime": current_bucket_naive,
            "open": float(open_),
            "high": float(high_),
            "low": float(low_),
            "close": float(close_),
            "volume": float(volume_),
            "value": float(value_),
            "venues": venues,
        },
        current_bucket_naive,
        api_minute_candles_for_db,
    )


def _normalize_intraday_rows(
    *,
    frame: pd.DataFrame,
    symbol: str,
    venue_config: _VenueConfig,
    target_day: date,
) -> list[_MinuteRow]:
    """
    Normalize KIS API intraday candle response to _MinuteRow objects.

    Parameters
    ----------
    frame : pd.DataFrame
        KIS API response DataFrame
    symbol : str
        Stock symbol
    venue_config : _VenueConfig
        Venue configuration for session boundaries
    target_day : date
        Target date for filtering

    Returns
    -------
    list[_MinuteRow]
        Normalized minute candle rows, sorted by time
    """
    if frame.empty:
        return []

    rows: list[_MinuteRow] = []
    for item in frame.to_dict("records"):
        raw_datetime = item.get("datetime")
        if raw_datetime is None:
            continue

        parsed = pd.to_datetime(str(raw_datetime), errors="coerce")
        if pd.isna(parsed):
            continue

        parsed_dt = parsed.to_pydatetime()
        local_dt = _ensure_kst_aware(parsed_dt).replace(second=0, microsecond=0)

        if local_dt.date() != target_day:
            continue

        local_clock = time(local_dt.hour, local_dt.minute, local_dt.second)
        if (
            local_clock < venue_config.session_start
            or local_clock > venue_config.session_end
        ):
            continue

        open_value = _parse_float(item.get("open"))
        high_value = _parse_float(item.get("high"))
        low_value = _parse_float(item.get("low"))
        close_value = _parse_float(item.get("close"))
        volume_value = _parse_float(item.get("volume"))
        value_value = _parse_float(item.get("value"))

        if (
            open_value is None
            or high_value is None
            or low_value is None
            or close_value is None
            or volume_value is None
            or value_value is None
        ):
            continue

        rows.append(
            _MinuteRow(
                minute_time=local_dt,
                venue=venue_config.venue,
                open=float(open_value),
                high=float(high_value),
                low=float(low_value),
                close=float(close_value),
                volume=float(volume_value),
                value=float(value_value),
            )
        )

    # Deduplicate by (minute_time, venue)
    deduped: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}
    for row in rows:
        deduped[(row.minute_time, row.venue)] = row
    return [deduped[key] for key in sorted(deduped)]


async def _fetch_historical_minutes_via_kis(
    *,
    symbol: str,
    end_date: datetime.date,
    limit: int,
) -> tuple[list[dict[str, object]], list[_MinuteRow]]:
    """
    KIS API를 통해 과거 1분봉 데이터를 조회하여 시간봉으로 집계

    Pagination을 사용하여 inquire_time_dailychartprice API를 호출하고,
    과거 데이터로 walk-back하며 충분한 분봉 데이터를 수집합니다.

    Parameters
    ----------
    symbol : str
        종목코드
    end_date : datetime.date
        조회 종료일
    limit : int
        가져올 시간봉 수

    Returns
    -------
    tuple[list[dict[str, object]], list[_MinuteRow]]
        - 시간봉 데이터 목록 (bucket, open, high, low, close, volume, value, venues)
        - 원본 1분봉 데이터 목록 (DB 저장용)
    """
    kis = KISClient()
    target_day = end_date

    # 목표: limit 시간 = limit * 60 분 데이터 수집
    target_minutes = limit * 60

    # 모든 venue의 분봉 데이터를 저장 (time_utc, venue) -> _MinuteRow
    all_minute_rows: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}

    # 초기 end_time: 장 마감 시간 (NTX 20:00, KRX 15:30)
    # 가장 늦은 시장 기준으로 시작 (NTX 20:00)
    end_time = "200000"

    page_calls = 0

    # Pagination loop: 최대 30페이지까지 호출
    for _ in range(_MAX_PAGE_CALLS_PER_DAY):
        # 충분한 데이터를 수집했으면 종료
        if len(all_minute_rows) >= target_minutes:
            logger.info(
                "Collected %d minutes (target: %d), stopping pagination",
                len(all_minute_rows),
                target_minutes,
            )
            break

        page_calls += 1

        # 각 venue별로 API 호출
        for venue_config in _VENUE_CONFIGS.values():
            try:
                frame = await kis.inquire_time_dailychartprice(
                    code=symbol,
                    market=venue_config.market_code,
                    n=200,
                    end_date=pd.Timestamp(target_day),
                    end_time=end_time,
                )

                if frame.empty:
                    continue

                # Normalize and merge rows
                page_rows = _normalize_intraday_rows(
                    frame=frame,
                    symbol=symbol,
                    venue_config=venue_config,
                    target_day=target_day,
                )

                # Add to all_minute_rows (deduplicated by time_utc and venue)
                for row in page_rows:
                    time_utc = _convert_kis_datetime_to_utc(row.minute_time)
                    key = (time_utc, row.venue)
                    all_minute_rows[key] = _MinuteRow(
                        minute_time=row.minute_time,
                        venue=row.venue,
                        open=row.open,
                        high=row.high,
                        low=row.low,
                        close=row.close,
                        volume=row.volume,
                        value=row.value,
                    )

            except Exception as e:
                # API 호출 실패 시 로그만 남기고 계속 진행
                logger.warning(
                    "KIS API call failed for %s %s at %s %s: %s",
                    symbol,
                    venue_config.venue,
                    target_day,
                    end_time,
                    e,
                )
                continue

        # 데이터를 수집하지 못했으면 종료
        if not all_minute_rows:
            logger.info(
                "No data collected from KIS API for %s on %s", symbol, target_day
            )
            break

        # 가장 이른 시간을 찾아서 다음 커서 계산 (walk backwards)
        earliest_local = min(row.minute_time for row in all_minute_rows.values())
        next_cursor = earliest_local - timedelta(minutes=1)

        # 세션 시작 시간 체크 (가장 이른 세션: NTX 08:00)
        if next_cursor.time() < time(8, 0, 0):
            logger.info(
                "Reached session boundary at %s for %s, stopping pagination",
                next_cursor,
                symbol,
            )
            break

        # 날짜가 바뀌면 종료
        if next_cursor.date() != target_day:
            logger.info(
                "Date boundary reached at %s for %s, stopping pagination",
                next_cursor,
                symbol,
            )
            break

        # 다음 커서 설정
        next_end_time = next_cursor.strftime("%H%M%S")
        if next_end_time == end_time:
            # 커서가 진전하지 않으면 무한 루프 방지
            logger.warning(
                "Cursor not progressing (end_time=%s), stopping pagination",
                end_time,
            )
            break
        end_time = next_end_time

    logger.info(
        "Pagination complete for %s: %d pages, %d minutes collected",
        symbol,
        page_calls,
        len(all_minute_rows),
    )

    if not all_minute_rows:
        return [], []

    # 1분봉을 시간봉으로 집계
    hourly_by_bucket: dict[datetime.datetime, dict[str, Any]] = {}

    for row in all_minute_rows.values():
        bucket_naive = _to_kst_naive(row.minute_time).replace(
            minute=0, second=0, microsecond=0
        )

        if bucket_naive not in hourly_by_bucket:
            hourly_by_bucket[bucket_naive] = {
                "minutes": [],
                "venues": set(),
            }

        hourly_by_bucket[bucket_naive]["minutes"].append(
            {
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
            }
        )
        hourly_by_bucket[bucket_naive]["venues"].add(row.venue)

    # 집계된 시간봉 생성
    hour_rows: list[dict[str, object]] = []

    for bucket_naive in sorted(hourly_by_bucket.keys(), reverse=True)[:limit]:
        data = hourly_by_bucket[bucket_naive]
        minutes = data["minutes"]

        if not minutes:
            continue

        open_ = minutes[0]["open"]
        high_ = max(m["high"] for m in minutes)
        low_ = min(m["low"] for m in minutes)
        close_ = minutes[-1]["close"]
        volume_ = sum(m["volume"] for m in minutes)
        value_ = sum(m["value"] for m in minutes)
        venues = _normalize_venues(list(data["venues"]))

        hour_rows.append(
            {
                "bucket": bucket_naive,
                "open": open_,
                "high": high_,
                "low": low_,
                "close": close_,
                "volume": volume_,
                "value": value_,
                "venues": venues,
            }
        )

    # Return both hourly aggregated data and original minute candles
    minute_rows_list = list(all_minute_rows.values())
    return hour_rows, minute_rows_list


def _log_task_exception(task: asyncio.Task[None]) -> None:
    """Callback to log exceptions from background storage tasks."""
    try:
        task.result()
    except Exception:
        logger.exception("Background minute candle storage task crashed")


async def _store_minute_candles_background(
    *,
    symbol: str,
    minute_rows: list[dict[str, object]],
) -> None:
    """
    Store minute candles to the database in the background (fire-and-forget).

    This function performs an upsert operation on the kr_candles_1m table.
    It is designed to be called as a background task using asyncio.create_task().

    Parameters
    ----------
    symbol : str
        Stock symbol (e.g., "005930" for Samsung Electronics)
    minute_rows : list[dict[str, object]]
        List of minute candle rows to upsert. Each row should contain:
        - time: datetime (KST naive)
        - venue: str ("KRX" or "NTX")
        - open, high, low, close: float
        - volume, value: float

    Notes
    -----
    - Uses ON CONFLICT DO UPDATE to handle duplicates gracefully
    - Errors are logged but not raised (fire-and-forget pattern)
    - Commits changes before returning to ensure data persistence
    """
    if not minute_rows:
        return

    try:
        async with _async_session() as session:
            for row in minute_rows:
                time_val = row.get("time")
                if not isinstance(time_val, datetime.datetime):
                    continue

                # Ensure time is KST naive (as stored in DB)
                time_naive = _to_kst_naive(time_val)

                await session.execute(
                    _UPSERT_SQL,
                    {
                        "symbol": symbol,
                        "time": time_naive,
                        "venue": str(row.get("venue", "KRX")),
                        "open": _to_float(row.get("open")),
                        "high": _to_float(row.get("high")),
                        "low": _to_float(row.get("low")),
                        "close": _to_float(row.get("close")),
                        "volume": _to_float(row.get("volume")),
                        "value": _to_float(row.get("value")),
                    },
                )

            await session.commit()
            logger.debug(
                "Stored %d minute candles for symbol '%s' in background",
                len(minute_rows),
                symbol,
            )

    except Exception as e:
        logger.error(
            "Failed to store minute candles for symbol '%s' in background: %s",
            symbol,
            e,
            exc_info=True,
        )


async def _fetch_minute_history_rows(
    *,
    symbol: str,
    end_time_kst: datetime.datetime,
    limit: int,
) -> list[dict[str, object]]:
    async with _async_session() as session:
        result = await session.execute(
            _KR_MINUTE_HISTORY_SQL,
            {
                "symbol": symbol,
                "end_time": end_time_kst,
                "limit": int(limit),
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


async def _fetch_intraday_history_rows(
    *,
    config: _IntradayPeriodConfig,
    symbol: str,
    end_time_kst: datetime.datetime,
    limit: int,
) -> list[dict[str, object]]:
    if config.period == "1m":
        return await _fetch_minute_history_rows(
            symbol=symbol,
            end_time_kst=end_time_kst,
            limit=max(limit * 4, limit),
        )

    query = text(
        f"""
        SELECT bucket, open, high, low, close, volume, value, venues
        FROM {config.history_table}
        WHERE symbol = :symbol
          AND bucket <= :end_time
        ORDER BY bucket DESC
        LIMIT :limit
        """
    )
    async with _async_session() as session:
        result = await session.execute(
            query,
            {
                "symbol": symbol,
                "end_time": end_time_kst,
                "limit": int(limit),
            },
        )
        return [{str(k): v for k, v in row.items()} for row in result.mappings().all()]


def _history_rows_to_frame(
    *,
    config: _IntradayPeriodConfig,
    rows: list[dict[str, object]],
) -> pd.DataFrame:
    if not rows:
        return _empty_intraday_frame()

    if config.period == "1m":
        minute_rows: list[_MinuteRow] = []
        for row in rows:
            time_raw = row.get("time")
            venue = _to_venue(row.get("venue"))
            if not isinstance(time_raw, datetime.datetime) or venue is None:
                continue
            assert venue is not None
            minute_rows.append(
                _MinuteRow(
                    minute_time=_to_kst_naive(time_raw).replace(
                        second=0, microsecond=0
                    ),
                    venue=venue,
                    open=_to_float(row.get("open")),
                    high=_to_float(row.get("high")),
                    low=_to_float(row.get("low")),
                    close=_to_float(row.get("close")),
                    volume=_to_float(row.get("volume")),
                    value=_to_float(row.get("value")),
                )
            )
        return _merge_minute_rows(minute_rows)

    frame_rows: list[dict[str, object]] = []
    for row in rows:
        bucket_raw = row.get("bucket")
        if not isinstance(bucket_raw, datetime.datetime):
            continue
        bucket_naive = _to_kst_naive(bucket_raw)
        session = _session_for_bucket_start(bucket_naive)
        if session is None:
            continue
        frame_rows.append(
            {
                "datetime": bucket_naive,
                "date": bucket_naive.date(),
                "time": bucket_naive.time(),
                "open": _to_float(row.get("open")),
                "high": _to_float(row.get("high")),
                "low": _to_float(row.get("low")),
                "close": _to_float(row.get("close")),
                "volume": _to_float(row.get("volume")),
                "value": _to_float(row.get("value")),
                "session": session,
                "venues": _normalize_venues(row.get("venues")),
            }
        )

    if not frame_rows:
        return _empty_intraday_frame()

    return (
        pd.DataFrame(frame_rows, columns=_INTRADAY_FRAME_COLUMNS)
        .sort_values("datetime")
        .reset_index(drop=True)
    )


async def _load_recent_overlay_frame(
    *,
    symbol: str,
    start_time_kst: datetime.datetime,
    end_time_kst: datetime.datetime,
    now_kst: datetime.datetime,
    nxt_eligible: bool,
    end_date: datetime.datetime | None,
) -> pd.DataFrame:
    start_naive = start_time_kst.replace(tzinfo=None)
    end_naive = end_time_kst.replace(tzinfo=None)

    minute_by_key: dict[tuple[datetime.datetime, VenueType], _MinuteRow] = {}
    db_rows = await _fetch_minute_rows(
        symbol=symbol,
        start_time_kst=start_time_kst,
        end_time_kst=end_time_kst,
    )
    for row in db_rows:
        time_raw = row.get("time")
        venue_opt = _to_venue(row.get("venue"))
        if not isinstance(time_raw, datetime.datetime) or venue_opt is None:
            continue
        venue = cast(VenueType, venue_opt)
        minute_time = _to_kst_naive(time_raw).replace(second=0, microsecond=0)
        if not (start_naive <= minute_time < end_naive):
            continue
        minute_by_key[(minute_time, venue)] = _MinuteRow(
            minute_time=minute_time,
            venue=venue,
            open=_to_float(row.get("open")),
            high=_to_float(row.get("high")),
            low=_to_float(row.get("low")),
            close=_to_float(row.get("close")),
            volume=_to_float(row.get("volume")),
            value=_to_float(row.get("value")),
        )

    markets = _api_markets_for_now(
        now_kst=now_kst,
        nxt_eligible=nxt_eligible,
        end_date=end_date,
    )
    if markets:
        kis = KISClient()
        api_end_date = pd.Timestamp(end_time_kst.date())
        legacy_end_time = end_time_kst.strftime("%H%M%S")

        async def _fetch_one(market: str) -> object:
            minute_chart = cast(Any, getattr(kis, "inquire_minute_chart", None))
            if callable(minute_chart):
                minute_chart_async = cast(
                    Callable[..., Awaitable[pd.DataFrame]],
                    minute_chart,
                )
                return await minute_chart_async(
                    code=symbol,
                    market=market,
                    time_unit=1,
                    n=30,
                    end_date=api_end_date,
                )
            return await kis.inquire_time_dailychartprice(
                code=symbol,
                market=market,
                n=30,
                end_date=api_end_date,
                end_time=legacy_end_time,
            )

        frames = await asyncio.gather(
            *[_fetch_one(market) for market in markets],
            return_exceptions=True,
        )
        for market, frame in zip(markets, frames, strict=False):
            if isinstance(frame, Exception):
                logger.warning(
                    "Recent overlay KIS API call failed for %s %s: %s",
                    symbol,
                    market,
                    frame,
                )
                continue
            if not isinstance(frame, pd.DataFrame) or frame.empty:
                continue
            if "datetime" not in frame.columns:
                continue

            venue: VenueType = "KRX" if market == "J" else "NTX"
            dt_series = pd.to_datetime(frame["datetime"], errors="coerce")
            for index, dt_value in enumerate(dt_series.tolist()):
                if pd.isna(dt_value):
                    continue
                minute_time = (
                    pd.Timestamp(dt_value)
                    .to_pydatetime()
                    .replace(second=0, microsecond=0)
                )
                if not (start_naive <= minute_time < end_naive):
                    continue
                src = frame.iloc[index]
                minute_by_key[(minute_time, venue)] = _MinuteRow(
                    minute_time=minute_time,
                    venue=venue,
                    open=_to_float(src.get("open")),
                    high=_to_float(src.get("high")),
                    low=_to_float(src.get("low")),
                    close=_to_float(src.get("close")),
                    volume=_to_float(src.get("volume")),
                    value=_to_float(src.get("value")),
                )

    return _merge_minute_rows(list(minute_by_key.values()))


async def read_kr_intraday_candles(
    *,
    symbol: str,
    period: str,
    count: int,
    end_date: datetime.datetime | None,
    now_kst: datetime.datetime | None = None,
) -> pd.DataFrame:
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

    if end_date is None:
        end_day = resolved_now.date()
        end_time_kst = resolved_now
    else:
        end_day = (
            _ensure_kst_aware(end_date).date() if end_date.tzinfo else end_date.date()
        )
        end_time_kst = datetime.datetime.combine(
            end_day,
            datetime.time(20, 0, 0),
            tzinfo=_KST,
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
        overlay_frame = await _load_recent_overlay_frame(
            symbol=universe.symbol,
            start_time_kst=overlay_start,
            end_time_kst=resolved_now + datetime.timedelta(minutes=1),
            now_kst=resolved_now,
            nxt_eligible=universe.nxt_eligible,
            end_date=end_date,
        )
        if not overlay_frame.empty:
            if config.bucket_minutes > 1:
                overlay_frame = _aggregate_minutes_to_buckets(
                    overlay_frame,
                    bucket_minutes=config.bucket_minutes,
                )
            touched = set(pd.to_datetime(overlay_frame["datetime"]).tolist())
            if not out.empty:
                out = out[~pd.to_datetime(out["datetime"]).isin(touched)]
            out = pd.concat([out, overlay_frame], ignore_index=True)

    if end_day == resolved_now.date() and len(out) < capped_count:
        _, api_minute_rows = await _fetch_historical_minutes_via_kis(
            symbol=universe.symbol,
            end_date=pd.Timestamp(end_day).date(),
            limit=capped_count,
        )
        if api_minute_rows:
            api_frame = _merge_minute_rows(api_minute_rows)
            if config.bucket_minutes > 1:
                api_frame = _aggregate_minutes_to_buckets(
                    api_frame,
                    bucket_minutes=config.bucket_minutes,
                )
            touched = set(pd.to_datetime(api_frame["datetime"]).tolist())
            if not out.empty:
                out = out[~pd.to_datetime(out["datetime"]).isin(touched)]
            out = pd.concat([out, api_frame], ignore_index=True)

    if out.empty:
        return _empty_intraday_frame()

    out = out.sort_values("datetime").reset_index(drop=True)
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

    Implements graceful degradation: returns partial or empty data instead of raising ValueError.
    Logs errors for debugging but never propagates exceptions to caller.

    Parameters
    ----------
    symbol : str
        Stock symbol (e.g., "005930" for Samsung Electronics)
    count : int
        Number of hourly candles to return
    end_date : datetime.datetime | None
        End date for query (None means current time)
    now_kst : datetime.datetime | None
        Current time in KST (None means now)

    Returns
    -------
    pd.DataFrame
        Hourly candles with columns: datetime, date, time, open, high, low, close, volume, value, session, venues
        Returns empty DataFrame if symbol not found or no data available (never raises ValueError)
    """
    capped_count = max(int(count), 1)
    resolved_now = _ensure_kst_aware(now_kst or datetime.datetime.now(_KST))

    # Resolve universe row - graceful degradation on error
    universe = await _resolve_universe_row(symbol)
    if isinstance(universe, _UniverseError):
        # Symbol not found or inactive - return empty DataFrame
        logger.info(
            "Symbol '%s' lookup failed: %s. Returning empty DataFrame.",
            symbol,
            universe.reason,
        )
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
            end_day,
            datetime.time(20, 0, 0),
            tzinfo=_KST,
        )

    fetch_limit = min(max(capped_count * 3, capped_count + 24), 1000)
    hour_rows = await _fetch_hour_rows(
        symbol=universe.symbol,
        end_time_kst=end_time_kst,
        limit=fetch_limit,
    )
    logger.info(
        "DB returned %d candles for symbol '%s' (requested %d)",
        len(hour_rows),
        universe.symbol,
        capped_count,
    )

    (
        current_hour_row,
        current_bucket_start,
        current_minute_candles,
    ) = await _build_current_hour_row(
        symbol=universe.symbol,
        now_kst=resolved_now,
        nxt_eligible=universe.nxt_eligible,
        end_date=end_date,
    )

    available_buckets = {
        _to_kst_naive(bucket_raw)
        for row in hour_rows
        if isinstance((bucket_raw := row.get("bucket")), datetime.datetime)
    }
    if current_bucket_start is not None:
        available_buckets.discard(current_bucket_start)
    if current_hour_row is not None:
        current_bucket_raw = current_hour_row.get("datetime")
        if isinstance(current_bucket_raw, datetime.datetime):
            available_buckets.add(_to_kst_naive(current_bucket_raw))

    # DB 데이터가 부족하면 KIS API fallback
    available_count = len(available_buckets)
    if available_count < capped_count:
        remaining = capped_count - available_count
        historical_limit = remaining + (1 if current_hour_row is not None else 0)
        logger.info(
            "Fallback to KIS API for symbol '%s': fetching %d missing candles",
            universe.symbol,
            remaining,
        )
        try:
            api_hour_rows, api_minute_rows = await _fetch_historical_minutes_via_kis(
                symbol=universe.symbol,
                end_date=end_time_kst.date(),
                limit=historical_limit,
            )
            # API 데이터 추가 (이미 DB에 있는 시간대는 제외)
            existing_buckets = {row.get("bucket") for row in hour_rows}
            for api_row in api_hour_rows:
                api_bucket = api_row.get("bucket")
                if (
                    current_bucket_start is not None
                    and api_bucket == current_bucket_start
                ):
                    continue
                if api_bucket not in existing_buckets:
                    hour_rows.append(api_row)

            # Store API minute candles for background storage
            fetched_minute_candles = api_minute_rows
        except Exception as e:
            # API fallback 실패 시 DB 데이터만 사용
            logger.warning(
                "KIS API fallback failed for symbol '%s': %s. Using DB data only.",
                symbol,
                e,
            )
            fetched_minute_candles = []
    else:
        fetched_minute_candles = []

    # Combine historical minute candles with current hour minute candles
    all_api_minute_candles = list(fetched_minute_candles)
    if current_minute_candles:
        all_api_minute_candles.extend(current_minute_candles)

    # Schedule background storage of API-fetched minute candles (fire-and-forget)
    if all_api_minute_candles:
        task = asyncio.create_task(
            _store_minute_candles_background(
                symbol=universe.symbol,
                minute_rows=[
                    {
                        "time": _convert_kis_datetime_to_utc(r.minute_time),
                        "venue": r.venue,
                        "open": r.open,
                        "high": r.high,
                        "low": r.low,
                        "close": r.close,
                        "volume": r.volume,
                        "value": r.value,
                    }
                    for r in all_api_minute_candles
                ],
            )
        )
        task.add_done_callback(_log_task_exception)
        logger.info(
            "Background task created to store %d minute candles for symbol '%s'",
            len(all_api_minute_candles),
            universe.symbol,
        )

    out = _build_hour_frame(
        hour_rows=hour_rows,
        current_hour_row=current_hour_row,
        count=capped_count,
        current_bucket_start=current_bucket_start,
    )

    # Return available data (DB-first with API fallback)
    # Graceful degradation: return partial or empty data instead of raising ValueError
    return out


__all__ = [
    "read_kr_hourly_candles_1h",
    "_store_minute_candles_background",
    "_log_task_exception",
]
