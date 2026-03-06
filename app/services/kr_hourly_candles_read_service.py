from __future__ import annotations

import asyncio
import datetime
import logging
from dataclasses import dataclass
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.services.brokers.kis.client import KISClient

_KST = ZoneInfo("Asia/Seoul")

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


def _to_kst_naive(value: datetime.datetime) -> datetime.datetime:
    return _ensure_kst_aware(value).replace(tzinfo=None)


def _to_float(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value))


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
    """
    Aggregate minute candles to hourly candles using OHLCV math.

    Parameters
    ----------
    df : pd.DataFrame
        Minute candles with columns: datetime, open, high, low, close, volume

    Returns
    -------
    pd.DataFrame
        Hourly candles with columns: datetime, open, high, low, close, volume
        Empty DataFrame if input is empty or invalid

    Notes
    -----
    - datetime is floored to hour (e.g., 2024-01-01 09:00:00)
    - open: first value in the hour
    - high: maximum value in the hour
    - low: minimum value in the hour
    - close: last value in the hour
    - volume: sum of volumes in the hour
    """
    if df.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    required_cols = {"datetime", "open", "high", "low", "close", "volume"}
    missing_cols = required_cols - set(df.columns)
    if missing_cols:
        logger.warning(
            "Missing required columns for aggregation: %s",
            sorted(missing_cols),
        )
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    out = df.copy()
    out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
    out = out.dropna(subset=["datetime"])

    if out.empty:
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    out["hour_bucket"] = out["datetime"].dt.floor("60min")

    try:
        aggregated = (
            out.groupby("hour_bucket", as_index=False)
            .agg(
                {
                    "open": "first",
                    "high": "max",
                    "low": "min",
                    "close": "last",
                    "volume": "sum",
                }
            )
            .sort_values("hour_bucket")
            .reset_index(drop=True)
        )
    except Exception as e:
        logger.error("Error during aggregation: %s", e)
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])

    result = aggregated.rename(columns={"hour_bucket": "datetime"})
    result = result[["datetime", "open", "high", "low", "close", "volume"]]
    result = result.reset_index(drop=True)

    return result


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
            return _UniverseError(reason=f"kr_symbol_universe is empty. {_kr_universe_sync_hint()}")
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
) -> tuple[dict[str, object] | None, datetime.datetime | None, list[dict[str, object]]]:
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
        venue = _to_venue(venue_raw)
        # Skip rows with invalid venue (graceful degradation)
        if venue is None:
            continue
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

    api_minute_candles_for_db: list[dict[str, object]] = []

    if markets:
        kis = KISClient()
        api_date = now_kst.date()

        async def _fetch_one(market: str) -> pd.DataFrame:
            return await kis.inquire_minute_chart(
                code=symbol,
                market=market,
                time_unit=1,
                n=30,
                end_date=api_date,
            )

        frames = await asyncio.gather(*[_fetch_one(m) for m in markets])
        for market, frame in zip(markets, frames, strict=False):
            if frame is None or frame.empty:
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
                    {
                        "time": minute_time,
                        "venue": venue,
                        "open": _to_float(src.get("open")),
                        "high": _to_float(src.get("high")),
                        "low": _to_float(src.get("low")),
                        "close": _to_float(src.get("close")),
                        "volume": _to_float(src.get("volume")),
                        "value": _to_float(src.get("value")),
                    }
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


async def _fetch_historical_minutes_via_kis(
    *,
    symbol: str,
    end_date: datetime.date,
    limit: int,
) -> list[dict[str, object]]:
    """
    KIS API를 통해 과거 1분봉 데이터를 조회하여 시간봉으로 집계

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
    list[dict[str, object]]
        시간봉 데이터 목록 (bucket, open, high, low, close, volume, value, venues)
    """
    kis = KISClient()

    # 1시간 = 60분, 여유있게 80분씩 요청
    n_minutes = min(limit * 80, 200)

    api_frames: list[pd.DataFrame] = []

    # KRX (J)와 NTX (NX) 시장에서 데이터 조회
    for market in ["J", "NX"]:
        try:
            frame = await kis.inquire_minute_chart(
                code=symbol,
                market=market,
                time_unit=1,
                n=n_minutes,
                end_date=end_date,
            )
            if frame is not None and not frame.empty:
                api_frames.append((market, frame))
        except Exception:
            # API 호출 실패 시 조용히 스킵
            pass

    if not api_frames:
        return []

    # 시간대별로 분봉 집계
    hourly_by_bucket: dict[datetime.datetime, dict[str, Any]] = {}

    for market, frame in api_frames:
        venue: VenueType = "KRX" if market == "J" else "NTX"

        if "datetime" not in frame.columns:
            continue

        for _, row in frame.iterrows():
            dt_raw = row.get("datetime")
            if pd.isna(dt_raw):
                continue

            dt = pd.Timestamp(dt_raw).to_pydatetime()
            dt_kst = _ensure_kst_aware(dt)
            bucket_naive = dt_kst.replace(minute=0, second=0, microsecond=0, tzinfo=None)

            # 장 시작 전 8시 이후, 장 마감 후 20시 이전만
            bucket_time = bucket_naive.time()
            if not (datetime.time(8, 0, 0) <= bucket_time <= datetime.time(20, 0, 0)):
                continue

            if bucket_naive not in hourly_by_bucket:
                hourly_by_bucket[bucket_naive] = {
                    "minutes": [],
                    "venues": set(),
                }

            hourly_by_bucket[bucket_naive]["minutes"].append(
                {
                    "open": _to_float(row.get("open")),
                    "high": _to_float(row.get("high")),
                    "low": _to_float(row.get("low")),
                    "close": _to_float(row.get("close")),
                    "volume": _to_float(row.get("volume")),
                    "value": _to_float(row.get("value")),
                }
            )
            hourly_by_bucket[bucket_naive]["venues"].add(venue)

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

    return hour_rows


def _log_task_exception(task: asyncio.Task) -> None:
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

    # DB 데이터가 부족하면 KIS API fallback
    available_count = len(hour_rows)
    if available_count < capped_count:
        remaining = capped_count - available_count
        logger.info(
            "Fallback to KIS API for symbol '%s': fetching %d missing candles",
            universe.symbol,
            remaining,
        )
        try:
            api_rows = await _fetch_historical_minutes_via_kis(
                symbol=universe.symbol,
                end_date=end_time_kst.date(),
                limit=remaining,
            )
            # API 데이터 추가 (이미 DB에 있는 시간대는 제외)
            existing_buckets = {row.get("bucket") for row in hour_rows}
            for api_row in api_rows:
                if api_row.get("bucket") not in existing_buckets:
                    hour_rows.append(api_row)
        except Exception as e:
            # API fallback 실패 시 DB 데이터만 사용
            logger.warning(
                "KIS API fallback failed for symbol '%s': %s. Using DB data only.",
                symbol,
                e,
            )

    current_hour_row, current_bucket_start, api_minute_candles = await _build_current_hour_row(
        symbol=universe.symbol,
        now_kst=resolved_now,
        nxt_eligible=universe.nxt_eligible,
        end_date=end_date,
    )

    # Schedule background storage of API-fetched minute candles (fire-and-forget)
    if api_minute_candles:
        task = asyncio.create_task(
            _store_minute_candles_background(
                symbol=universe.symbol,
                minute_rows=api_minute_candles,
            )
        )
        task.add_done_callback(_log_task_exception)
        logger.info(
            "Background task created to store %d minute candles for symbol '%s'",
            len(api_minute_candles),
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


__all__ = ["read_kr_hourly_candles_1h", "_store_minute_candles_background", "_log_task_exception"]
