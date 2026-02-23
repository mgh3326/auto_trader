from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from functools import lru_cache
from typing import Literal, cast
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.manual_holdings import MarketType
from app.services.brokers.kis.client import KISClient
from app.services.manual_holdings_service import ManualHoldingsService

logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
_OVERLAP_MINUTES = 5
_DEFAULT_BOOTSTRAP_SESSIONS = 10
_MAX_PAGE_CALLS_PER_DAY = 30


@dataclass(frozen=True, slots=True)
class VenueConfig:
    venue: str
    market_code: str
    session_start: time
    session_end: time


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    time_utc: datetime
    local_time: datetime
    symbol: str
    venue: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


_VENUE_CONFIG: dict[str, VenueConfig] = {
    "KRX": VenueConfig(
        venue="KRX",
        market_code="J",
        session_start=time(9, 0, 0),
        session_end=time(15, 30, 0),
    ),
    "NTX": VenueConfig(
        venue="NTX",
        market_code="NX",
        session_start=time(8, 0, 0),
        session_end=time(20, 0, 0),
    ),
}

_CURSOR_SQL = text(
    """
    SELECT MAX(time)
    FROM public.kr_candles_1m
    WHERE symbol = :symbol
      AND venue = :venue
    """
)

_UPSERT_SQL = text(
    """
    INSERT INTO public.kr_candles_1m
        (time, symbol, venue, open, high, low, close, volume, value)
    VALUES
        (:time, :symbol, :venue, :open, :high, :low, :close, :volume, :value)
    ON CONFLICT (time, symbol, venue)
    DO UPDATE SET
        open = EXCLUDED.open,
        high = EXCLUDED.high,
        low = EXCLUDED.low,
        close = EXCLUDED.close,
        volume = EXCLUDED.volume,
        value = EXCLUDED.value
    WHERE
        kr_candles_1m.open IS DISTINCT FROM EXCLUDED.open
        OR kr_candles_1m.high IS DISTINCT FROM EXCLUDED.high
        OR kr_candles_1m.low IS DISTINCT FROM EXCLUDED.low
        OR kr_candles_1m.close IS DISTINCT FROM EXCLUDED.close
        OR kr_candles_1m.volume IS DISTINCT FROM EXCLUDED.volume
        OR kr_candles_1m.value IS DISTINCT FROM EXCLUDED.value
    """
)


@lru_cache(maxsize=1)
def _get_xkrx_calendar():
    return xcals.get_calendar("XKRX")


def _normalize_mode(mode: str) -> Literal["incremental", "backfill"]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"incremental", "backfill"}:
        raise ValueError("mode must be 'incremental' or 'backfill'")
    return cast(Literal["incremental", "backfill"], normalized)


def _normalize_symbol(value: object) -> str | None:
    text_value = str(value or "").strip().upper()
    if not text_value:
        return None
    if len(text_value) < 6:
        text_value = text_value.zfill(6)
    if len(text_value) == 6 and text_value.isalnum():
        return text_value
    return None


def _parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _build_symbol_union(
    kis_holdings: Sequence[object],
    manual_holdings: Sequence[object],
) -> set[str]:
    symbols: set[str] = set()

    for item in kis_holdings:
        if isinstance(item, dict):
            raw_symbol = cast(object | None, item.get("pdno"))
        else:
            raw_symbol = getattr(item, "pdno", None)
        symbol = _normalize_symbol(raw_symbol)
        if symbol is not None:
            symbols.add(symbol)

    for holding in manual_holdings:
        ticker = getattr(holding, "ticker", None)
        symbol = _normalize_symbol(ticker)
        if symbol is not None:
            symbols.add(symbol)

    return symbols


def _validate_universe_rows(
    *,
    target_symbols: set[str],
    universe_rows: list[KRSymbolUniverse],
    table_has_rows: bool,
) -> dict[str, KRSymbolUniverse]:
    if not table_has_rows:
        raise ValueError("kr_symbol_universe is empty")

    rows_by_symbol = {row.symbol: row for row in universe_rows}
    missing = sorted(target_symbols - set(rows_by_symbol))
    if missing:
        preview = ", ".join(missing[:10])
        raise ValueError(
            f"KR symbol is not registered in kr_symbol_universe: "
            f"count={len(missing)} symbols=[{preview}]"
        )

    inactive = sorted(
        symbol
        for symbol in target_symbols
        if symbol in rows_by_symbol and not rows_by_symbol[symbol].is_active
    )
    if inactive:
        preview = ", ".join(inactive[:10])
        raise ValueError(
            f"KR symbol is inactive in kr_symbol_universe: "
            f"count={len(inactive)} symbols=[{preview}]"
        )

    return {symbol: rows_by_symbol[symbol] for symbol in target_symbols}


def _build_venue_plan(
    rows_by_symbol: dict[str, KRSymbolUniverse],
) -> dict[str, list[VenueConfig]]:
    plan: dict[str, list[VenueConfig]] = {}
    for symbol in sorted(rows_by_symbol):
        row = rows_by_symbol[symbol]
        if row.nxt_eligible:
            plan[symbol] = [_VENUE_CONFIG["KRX"], _VENUE_CONFIG["NTX"]]
        else:
            plan[symbol] = [_VENUE_CONFIG["KRX"]]
    return plan


def _is_session_day_kst(target_day: date) -> bool:
    calendar = _get_xkrx_calendar()
    return bool(calendar.is_session(pd.Timestamp(target_day)))


def _should_process_venue(
    *,
    mode: Literal["incremental", "backfill"],
    now_kst: datetime,
    is_session_day: bool,
    venue: VenueConfig,
) -> tuple[bool, str | None]:
    if mode == "backfill":
        return True, None

    if not is_session_day:
        return False, "holiday"

    now_clock = time(now_kst.hour, now_kst.minute, now_kst.second)
    if now_clock < venue.session_start or now_clock > venue.session_end:
        return False, "outside_session"

    return True, None


def _compute_incremental_cutoff_kst(cursor_utc: datetime | None) -> datetime | None:
    if cursor_utc is None:
        return None

    if cursor_utc.tzinfo is None:
        normalized_cursor = cursor_utc.replace(tzinfo=UTC)
    else:
        normalized_cursor = cursor_utc.astimezone(UTC)

    return normalized_cursor.astimezone(_KST) - timedelta(minutes=_OVERLAP_MINUTES)


def _convert_kis_datetime_to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        localized = value.replace(tzinfo=_KST)
    else:
        localized = value.astimezone(_KST)
    return localized.astimezone(UTC)


def _recent_session_days(
    now_kst: datetime,
    sessions: int,
    *,
    include_today: bool,
) -> list[date]:
    calendar = _get_xkrx_calendar()
    lookback_days = max(90, sessions * 8)
    start = pd.Timestamp(now_kst.date() - timedelta(days=lookback_days))
    end = pd.Timestamp(now_kst.date())
    session_index = calendar.sessions_in_range(start, end)
    days = [pd.Timestamp(value).date() for value in session_index]
    if not include_today and days and days[-1] == now_kst.date():
        days = days[:-1]
    if not days:
        return []
    return days[-sessions:]


def _day_before_cutoff(
    *,
    target_day: date,
    venue: VenueConfig,
    cutoff_kst: datetime | None,
) -> bool:
    if cutoff_kst is None:
        return False
    day_end = datetime.combine(target_day, venue.session_end, tzinfo=_KST)
    return day_end < cutoff_kst


def _initial_end_time(now_kst: datetime, target_day: date, venue: VenueConfig) -> str:
    close_hhmmss = venue.session_end.strftime("%H%M%S")
    if target_day < now_kst.date():
        return close_hhmmss
    now_hhmmss = now_kst.strftime("%H%M%S")
    return min(now_hhmmss, close_hhmmss)


def _normalize_intraday_rows(
    *,
    frame: pd.DataFrame,
    symbol: str,
    venue: VenueConfig,
    target_day: date,
) -> list[MinuteCandleRow]:
    if frame.empty:
        return []

    rows: list[MinuteCandleRow] = []
    for item in frame.to_dict("records"):
        raw_datetime = item.get("datetime")
        if raw_datetime is None:
            continue

        parsed = pd.to_datetime(str(raw_datetime), errors="coerce")
        if pd.isna(parsed):
            continue

        parsed_dt = parsed.to_pydatetime()
        if parsed_dt.tzinfo is None:
            local_dt = parsed_dt.replace(tzinfo=_KST)
        else:
            local_dt = parsed_dt.astimezone(_KST)
        local_dt = local_dt.replace(second=0, microsecond=0)

        if local_dt.date() != target_day:
            continue

        local_clock = time(local_dt.hour, local_dt.minute, local_dt.second)
        if local_clock < venue.session_start or local_clock > venue.session_end:
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
            MinuteCandleRow(
                time_utc=_convert_kis_datetime_to_utc(local_dt),
                local_time=local_dt,
                symbol=symbol,
                venue=venue.venue,
                open=float(open_value),
                high=float(high_value),
                low=float(low_value),
                close=float(close_value),
                volume=float(volume_value),
                value=float(value_value),
            )
        )

    deduped: dict[datetime, MinuteCandleRow] = {}
    for row in rows:
        deduped[row.time_utc] = row
    return [deduped[key] for key in sorted(deduped)]


async def _read_cursor_utc(
    session: AsyncSession,
    *,
    symbol: str,
    venue: str,
) -> datetime | None:
    result = await session.execute(_CURSOR_SQL, {"symbol": symbol, "venue": venue})
    value = result.scalar_one_or_none()
    if isinstance(value, datetime):
        return value
    return None


async def _upsert_rows(session: AsyncSession, rows: list[MinuteCandleRow]) -> int:
    if not rows:
        return 0

    payload = [
        {
            "time": row.time_utc,
            "symbol": row.symbol,
            "venue": row.venue,
            "open": row.open,
            "high": row.high,
            "low": row.low,
            "close": row.close,
            "volume": row.volume,
            "value": row.value,
        }
        for row in rows
    ]
    _ = await session.execute(_UPSERT_SQL, payload)
    return len(payload)


async def _collect_day_rows(
    *,
    kis: KISClient,
    symbol: str,
    venue: VenueConfig,
    target_day: date,
    initial_end_time: str,
    cutoff_kst: datetime | None,
) -> tuple[list[MinuteCandleRow], int, bool, bool]:
    merged: dict[datetime, MinuteCandleRow] = {}
    end_time = initial_end_time
    page_calls = 0
    reached_cutoff = False

    for _ in range(_MAX_PAGE_CALLS_PER_DAY):
        page_calls += 1
        frame = await kis.inquire_time_dailychartprice(
            code=symbol,
            market=venue.market_code,
            n=200,
            end_date=target_day,
            end_time=end_time,
        )
        if frame.empty:
            ordered = [merged[key] for key in sorted(merged)]
            return ordered, page_calls, reached_cutoff, True

        page_rows = _normalize_intraday_rows(
            frame=frame,
            symbol=symbol,
            venue=venue,
            target_day=target_day,
        )
        if not page_rows:
            ordered = [merged[key] for key in sorted(merged)]
            return ordered, page_calls, reached_cutoff, True

        earliest_local = min(row.local_time for row in page_rows)

        for row in page_rows:
            if cutoff_kst is not None and row.local_time < cutoff_kst:
                reached_cutoff = True
                continue
            merged[row.time_utc] = row

        next_cursor = earliest_local - timedelta(minutes=1)
        if cutoff_kst is not None and next_cursor < cutoff_kst:
            reached_cutoff = True

        if reached_cutoff:
            break

        if next_cursor.date() != target_day:
            break

        next_clock = time(next_cursor.hour, next_cursor.minute, next_cursor.second)
        if next_clock < venue.session_start:
            break

        next_end_time = next_cursor.strftime("%H%M%S")
        if next_end_time == end_time:
            break
        end_time = next_end_time

    ordered = [merged[key] for key in sorted(merged)]
    return ordered, page_calls, reached_cutoff, False


async def _sync_symbol_venue(
    *,
    session: AsyncSession,
    kis: KISClient,
    symbol: str,
    venue: VenueConfig,
    mode: Literal["incremental", "backfill"],
    now_kst: datetime,
    backfill_days: list[date] | None,
) -> dict[str, int | bool | str]:
    cursor_utc = await _read_cursor_utc(session, symbol=symbol, venue=venue.venue)
    cutoff_kst = _compute_incremental_cutoff_kst(cursor_utc)

    if mode == "backfill":
        if not backfill_days:
            return {
                "rows_upserted": 0,
                "days_processed": 0,
                "pages_fetched": 0,
                "empty_response": True,
            }
        earliest_day = backfill_days[0]
        cutoff_kst = datetime.combine(earliest_day, venue.session_start, tzinfo=_KST)
        allowed_days: set[date] | None = set(backfill_days)
    else:
        if cutoff_kst is None:
            bootstrap_days = _recent_session_days(
                now_kst,
                _DEFAULT_BOOTSTRAP_SESSIONS,
                include_today=True,
            )
            if bootstrap_days:
                cutoff_kst = datetime.combine(
                    bootstrap_days[0],
                    venue.session_start,
                    tzinfo=_KST,
                )
        allowed_days = None

    if cutoff_kst is not None and cutoff_kst > now_kst:
        cutoff_kst = now_kst

    rows_upserted = 0
    pages_fetched = 0
    days_processed = 0
    saw_empty_response = False
    current_day = now_kst.date()

    while True:
        if _day_before_cutoff(
            target_day=current_day, venue=venue, cutoff_kst=cutoff_kst
        ):
            break

        if allowed_days is not None:
            if current_day < min(allowed_days):
                break
            if current_day not in allowed_days:
                current_day = current_day - timedelta(days=1)
                continue

        if not _is_session_day_kst(current_day):
            current_day = current_day - timedelta(days=1)
            continue

        initial_end_time = _initial_end_time(now_kst, current_day, venue)
        day_rows, page_calls, reached_cutoff, empty_response = await _collect_day_rows(
            kis=kis,
            symbol=symbol,
            venue=venue,
            target_day=current_day,
            initial_end_time=initial_end_time,
            cutoff_kst=cutoff_kst,
        )
        pages_fetched += page_calls
        days_processed += 1

        if day_rows:
            rows_upserted += await _upsert_rows(session, day_rows)
        elif empty_response:
            saw_empty_response = True
            logger.warning(
                "KR candles sync empty response symbol=%s venue=%s day=%s end_time=%s",
                symbol,
                venue.venue,
                current_day.isoformat(),
                initial_end_time,
            )

        if reached_cutoff:
            break

        if empty_response:
            current_day = current_day - timedelta(days=1)
            continue

        current_day = current_day - timedelta(days=1)

    return {
        "rows_upserted": rows_upserted,
        "days_processed": days_processed,
        "pages_fetched": pages_fetched,
        "empty_response": saw_empty_response,
    }


async def _load_universe_context(
    session: AsyncSession,
    target_symbols: set[str],
) -> tuple[list[KRSymbolUniverse], bool]:
    has_rows_result = await session.execute(select(KRSymbolUniverse.symbol).limit(1))
    table_has_rows = has_rows_result.scalar_one_or_none() is not None

    if not target_symbols:
        return [], table_has_rows

    result = await session.execute(
        select(KRSymbolUniverse).where(KRSymbolUniverse.symbol.in_(target_symbols))
    )
    rows = list(result.scalars().all())
    return rows, table_has_rows


async def sync_kr_candles(
    *,
    mode: str,
    sessions: int = 10,
    user_id: int = 1,
) -> dict[str, object]:
    normalized_mode = _normalize_mode(mode)
    session_count = max(int(sessions), 1)
    now_kst = datetime.now(_KST)
    session_day_today = _is_session_day_kst(now_kst.date())

    kis = KISClient()

    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        kis_holdings = await kis.fetch_my_stocks()
        manual_service = ManualHoldingsService(session)
        manual_holdings = await manual_service.get_holdings_by_user(
            user_id=user_id,
            market_type=MarketType.KR,
        )
        target_symbols = _build_symbol_union(kis_holdings, manual_holdings)
        if not target_symbols:
            return {
                "mode": normalized_mode,
                "sessions": session_count,
                "skipped": True,
                "reason": "no_target_symbols",
                "symbols_total": 0,
                "symbol_venues_total": 0,
                "pairs_processed": 0,
                "pairs_skipped": 0,
                "rows_upserted": 0,
                "pages_fetched": 0,
            }

        universe_rows, table_has_rows = await _load_universe_context(
            session,
            target_symbols,
        )
        rows_by_symbol = _validate_universe_rows(
            target_symbols=target_symbols,
            universe_rows=universe_rows,
            table_has_rows=table_has_rows,
        )
        venue_plan = _build_venue_plan(rows_by_symbol)

        backfill_days: list[date] | None = None
        if normalized_mode == "backfill":
            include_today = now_kst.time() >= _VENUE_CONFIG["KRX"].session_end
            backfill_days = _recent_session_days(
                now_kst,
                session_count,
                include_today=include_today,
            )

        pairs_total = sum(len(venues) for venues in venue_plan.values())
        pairs_processed = 0
        pairs_skipped = 0
        rows_upserted = 0
        pages_fetched = 0
        skipped_reasons: dict[str, int] = {}

        for symbol, venues in venue_plan.items():
            for venue in venues:
                should_process, skip_reason = _should_process_venue(
                    mode=normalized_mode,
                    now_kst=now_kst,
                    is_session_day=session_day_today,
                    venue=venue,
                )
                if not should_process:
                    pairs_skipped += 1
                    if skip_reason is not None:
                        skipped_reasons[skip_reason] = (
                            skipped_reasons.get(skip_reason, 0) + 1
                        )
                    continue

                try:
                    stats = await _sync_symbol_venue(
                        session=session,
                        kis=kis,
                        symbol=symbol,
                        venue=venue,
                        mode=normalized_mode,
                        now_kst=now_kst,
                        backfill_days=backfill_days,
                    )
                    await session.commit()
                except Exception:
                    await session.rollback()
                    raise

                pairs_processed += 1
                rows_upserted += int(stats["rows_upserted"])
                pages_fetched += int(stats["pages_fetched"])

        skipped = pairs_processed == 0
        return {
            "mode": normalized_mode,
            "sessions": session_count,
            "skipped": skipped,
            "skip_reasons": skipped_reasons,
            "symbols_total": len(target_symbols),
            "symbol_venues_total": pairs_total,
            "pairs_processed": pairs_processed,
            "pairs_skipped": pairs_skipped,
            "rows_upserted": rows_upserted,
            "pages_fetched": pages_fetched,
        }
    finally:
        await session.close()


__all__ = ["sync_kr_candles"]
