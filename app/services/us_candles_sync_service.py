# pyright: reportMissingTypeStubs=none
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Literal, Protocol, cast
from zoneinfo import ZoneInfo

import exchange_calendars as xcals
import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.core.symbol import to_db_symbol
from app.models.manual_holdings import MarketType
from app.services.brokers.kis.client import KISClient
from app.services.manual_holdings_service import ManualHoldingsService
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

_NY = ZoneInfo("America/New_York")
_OVERLAP_MINUTES = 5
_PAGE_SIZE = 120

type TimestampLike = datetime | pd.Timestamp | str


class MinuteChartPage(Protocol):
    frame: pd.DataFrame
    has_more: bool
    next_keyb: str | None


class _RowcountResult(Protocol):
    rowcount: int | None


class MinuteChartSource(Protocol):
    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 120,
        keyb: str = "",
    ) -> MinuteChartPage: ...


_CURSOR_SQL = text(
    """
    SELECT MAX(time)
    FROM public.us_candles_1m
    WHERE symbol = :symbol
      AND exchange = :exchange
    """
)

_UPSERT_SQL = text(
    """
    INSERT INTO public.us_candles_1m
        (time, symbol, exchange, open, high, low, close, volume, value)
    VALUES
        (:time, :symbol, :exchange, :open, :high, :low, :close, :volume, :value)
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

_EXISTING_ROWS_SQL = text(
    """
    SELECT time, open, high, low, close, volume, value
    FROM public.us_candles_1m
    WHERE symbol = :symbol
      AND exchange = :exchange
      AND time >= :start_time
      AND time <= :end_time
    """
)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    session: pd.Timestamp
    open_utc: datetime
    close_utc: datetime
    last_minute_utc: datetime


@dataclass(frozen=True, slots=True)
class MinuteCandleRow:
    time_utc: datetime
    local_time: datetime
    symbol: str
    exchange: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float


class OverseasMinuteChartPageProtocol(Protocol):
    frame: pd.DataFrame
    has_more: bool
    next_keyb: str | None


class OverseasMinuteChartClientProtocol(Protocol):
    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 120,
        keyb: str = "",
    ) -> OverseasMinuteChartPageProtocol: ...


@lru_cache(maxsize=1)
def _get_xnys_calendar():
    return xcals.get_calendar("XNYS", side="left")


def _utc_now_floor_minute() -> pd.Timestamp:
    return pd.Timestamp(datetime.now(UTC)).floor("min")


def _normalize_mode(mode: str) -> Literal["incremental", "backfill"]:
    normalized = str(mode or "").strip().lower()
    if normalized not in {"incremental", "backfill"}:
        raise ValueError("mode must be 'incremental' or 'backfill'")
    return cast(Literal["incremental", "backfill"], normalized)


def _normalize_symbol(value: object) -> str | None:
    normalized = to_db_symbol(str(value or "").strip().upper())
    return normalized or None


def _parse_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _to_utc_datetime(value: datetime | pd.Timestamp | str) -> datetime:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(UTC)
    else:
        timestamp = timestamp.tz_convert(UTC)
    return timestamp.to_pydatetime().astimezone(UTC)


def _to_local_minute(value: datetime | pd.Timestamp | str | None) -> datetime | None:
    if value is None:
        return None
    try:
        timestamp = pd.Timestamp(value)
    except (TypeError, ValueError):
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize(_NY)
    else:
        timestamp = timestamp.tz_convert(_NY)
    local_dt = timestamp.to_pydatetime().astimezone(_NY)
    return local_dt.replace(second=0, microsecond=0)


def _build_symbol_union(
    kis_holdings: Sequence[object],
    manual_holdings: Sequence[object],
) -> set[str]:
    symbols: set[str] = set()

    for item in kis_holdings:
        raw_symbol = (
            cast(dict[str, object], item).get("ovrs_pdno")
            if isinstance(item, dict)
            else getattr(item, "ovrs_pdno", None)
        )
        symbol = _normalize_symbol(raw_symbol)
        if symbol is not None:
            symbols.add(symbol)

    for holding in manual_holdings:
        symbol = _normalize_symbol(getattr(holding, "ticker", None))
        if symbol is not None:
            symbols.add(symbol)

    return symbols


def _select_closed_sessions(now_utc: datetime, sessions: int) -> list[SessionWindow]:
    calendar = _get_xnys_calendar()
    count = max(int(sessions), 1)
    now_ts = pd.Timestamp(now_utc)
    last_closed = calendar.minute_to_past_session(now_ts, count=1)
    session_index = calendar.sessions_window(last_closed, -count)
    selected = list(pd.DatetimeIndex(session_index)[-count:])

    windows: list[SessionWindow] = []
    for session in selected:
        open_utc = _to_utc_datetime(calendar.session_open(session))
        close_utc = _to_utc_datetime(calendar.session_close(session))
        windows.append(
            SessionWindow(
                session=pd.Timestamp(session),
                open_utc=open_utc,
                close_utc=close_utc,
                last_minute_utc=close_utc - timedelta(minutes=1),
            )
        )
    return windows


def _compute_incremental_lower_bound(
    cursor_utc: datetime | None,
    session_open_utc: datetime,
) -> datetime:
    if cursor_utc is None:
        return session_open_utc

    normalized_cursor = (
        cursor_utc if cursor_utc.tzinfo is not None else cursor_utc.replace(tzinfo=UTC)
    )
    overlapped = normalized_cursor.astimezone(UTC) - timedelta(minutes=_OVERLAP_MINUTES)
    return max(overlapped, session_open_utc)


def _normalize_minute_page(
    *,
    frame: pd.DataFrame,
    symbol: str,
    exchange: str,
    lower_bound_utc: datetime,
    upper_bound_utc: datetime,
) -> list[MinuteCandleRow]:
    if frame.empty:
        return []

    deduped: dict[datetime, MinuteCandleRow] = {}
    for item in frame.to_dict("records"):
        local_dt = _to_local_minute(item.get("datetime"))
        if local_dt is None:
            continue

        time_utc = local_dt.astimezone(UTC)
        if time_utc < lower_bound_utc or time_utc > upper_bound_utc:
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

        deduped[time_utc] = MinuteCandleRow(
            time_utc=time_utc,
            local_time=local_dt,
            symbol=symbol,
            exchange=exchange,
            open=float(open_value),
            high=float(high_value),
            low=float(low_value),
            close=float(close_value),
            volume=float(volume_value),
            value=float(value_value),
        )

    return [deduped[key] for key in sorted(deduped)]


def _extract_earliest_utc(frame: pd.DataFrame) -> datetime | None:
    if frame.empty or "datetime" not in frame:
        return None

    parsed_values = pd.to_datetime(frame["datetime"], errors="coerce")
    cleaned = [value for value in parsed_values.tolist() if not pd.isna(value)]
    if not cleaned:
        return None

    earliest = min(pd.Timestamp(value) for value in cleaned)
    if earliest.tzinfo is None:
        earliest = earliest.tz_localize(_NY)
    else:
        earliest = earliest.tz_convert(_NY)
    return (
        earliest.tz_convert(UTC)
        .to_pydatetime()
        .astimezone(UTC)
        .replace(second=0, microsecond=0)
    )


def _parse_keyb_to_utc(keyb: str) -> datetime | None:
    cleaned = str(keyb).strip()
    if not cleaned:
        return None
    parsed = pd.to_datetime(cleaned, format="%Y%m%d%H%M%S", errors="coerce")
    if pd.isna(parsed):
        return None
    timestamp = pd.Timestamp(parsed).tz_localize(_NY).tz_convert(UTC)
    return timestamp.to_pydatetime().astimezone(UTC).replace(second=0, microsecond=0)


async def _read_cursor_utc(
    session: AsyncSession,
    *,
    symbol: str,
    exchange: str,
) -> datetime | None:
    result = await session.execute(
        _CURSOR_SQL, {"symbol": symbol, "exchange": exchange}
    )
    value = result.scalar_one_or_none()
    return value if isinstance(value, datetime) else None


async def _upsert_rows(session: AsyncSession, rows: list[MinuteCandleRow]) -> int:
    if not rows:
        return 0

    symbol = rows[0].symbol
    exchange = rows[0].exchange
    start_time = min(row.time_utc for row in rows)
    end_time = max(row.time_utc for row in rows)

    existing_result = await session.execute(
        _EXISTING_ROWS_SQL,
        {
            "symbol": symbol,
            "exchange": exchange,
            "start_time": start_time,
            "end_time": end_time,
        },
    )
    existing_rows = {
        mapping["time"]: (
            float(mapping["open"]),
            float(mapping["high"]),
            float(mapping["low"]),
            float(mapping["close"]),
            float(mapping["volume"]),
            float(mapping["value"]),
        )
        for mapping in existing_result.mappings().all()
    }

    payload = []
    for row in rows:
        current_values = (
            row.open,
            row.high,
            row.low,
            row.close,
            row.volume,
            row.value,
        )
        if existing_rows.get(row.time_utc) == current_values:
            continue

        payload.append(
            {
                "time": row.time_utc,
                "symbol": row.symbol,
                "exchange": row.exchange,
                "open": row.open,
                "high": row.high,
                "low": row.low,
                "close": row.close,
                "volume": row.volume,
                "value": row.value,
            }
        )

    if not payload:
        return 0

    result = cast(
        _RowcountResult, cast(object, await session.execute(_UPSERT_SQL, payload))
    )
    return max(int(result.rowcount or 0), 0)


async def _collect_window_rows(
    *,
    kis: OverseasMinuteChartClientProtocol,
    symbol: str,
    exchange: str,
    lower_bound_utc: datetime,
    upper_bound_utc: datetime,
) -> tuple[list[MinuteCandleRow], int]:
    merged: dict[datetime, MinuteCandleRow] = {}
    page_calls = 0
    current_keyb = upper_bound_utc.astimezone(_NY).strftime("%Y%m%d%H%M%S")

    while True:
        page_calls += 1
        page = await kis.inquire_overseas_minute_chart(
            symbol,
            exchange_code=exchange,
            n=_PAGE_SIZE,
            keyb=current_keyb,
        )

        if page.frame.empty:
            break

        for row in _normalize_minute_page(
            frame=page.frame,
            symbol=symbol,
            exchange=exchange,
            lower_bound_utc=lower_bound_utc,
            upper_bound_utc=upper_bound_utc,
        ):
            merged[row.time_utc] = row

        earliest_utc = _extract_earliest_utc(page.frame)
        if earliest_utc is not None and earliest_utc <= lower_bound_utc:
            break

        next_keyb = str(page.next_keyb or "").strip()
        if not page.has_more or not next_keyb or next_keyb == current_keyb:
            break
        next_keyb_utc = _parse_keyb_to_utc(next_keyb)
        if next_keyb_utc is not None and next_keyb_utc < lower_bound_utc:
            break
        current_keyb = next_keyb

    return [merged[key] for key in sorted(merged)], page_calls


async def sync_us_candles(
    *,
    mode: str,
    sessions: int = 10,
    user_id: int = 1,
) -> dict[str, object]:
    normalized_mode = _normalize_mode(mode)
    session_count = max(int(sessions), 1)
    now_utc = _utc_now_floor_minute().to_pydatetime().astimezone(UTC)
    calendar = _get_xnys_calendar()
    kis = KISClient()

    session = cast(AsyncSession, cast(object, AsyncSessionLocal()))
    try:
        manual_service = ManualHoldingsService(session)
        kis_holdings = await kis.fetch_my_us_stocks()
        manual_holdings = await manual_service.get_holdings_by_user(
            user_id=user_id,
            market_type=MarketType.US,
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

        symbol_pairs = [
            (symbol, await get_us_exchange_by_symbol(symbol, db=session))
            for symbol in sorted(target_symbols)
        ]
        pairs_total = len(symbol_pairs)

        windows: list[SessionWindow]
        if normalized_mode == "incremental":
            if not calendar.is_trading_minute(pd.Timestamp(now_utc)):
                return {
                    "mode": normalized_mode,
                    "sessions": session_count,
                    "skipped": True,
                    "skip_reasons": {"outside_trading_minute": len(symbol_pairs)},
                    "symbols_total": len(target_symbols),
                    "symbol_venues_total": pairs_total,
                    "pairs_processed": 0,
                    "pairs_skipped": pairs_total,
                    "rows_upserted": 0,
                    "pages_fetched": 0,
                }

            current_session = calendar.minute_to_session(
                pd.Timestamp(now_utc), direction="none"
            )
            session_open_utc = _to_utc_datetime(calendar.session_open(current_session))
            session_close_utc = _to_utc_datetime(
                calendar.session_close(current_session)
            )
            windows = [
                SessionWindow(
                    session=pd.Timestamp(current_session),
                    open_utc=session_open_utc,
                    close_utc=session_close_utc,
                    last_minute_utc=min(
                        session_close_utc - timedelta(minutes=1), now_utc
                    ),
                )
            ]
        else:
            windows = _select_closed_sessions(now_utc, session_count)

        pairs_processed = 0
        rows_upserted = 0
        pages_fetched = 0
        pairs_skipped = 0

        for symbol, exchange in symbol_pairs:
            pair_rows: list[MinuteCandleRow] = []
            pair_pages = 0
            for window in windows:
                lower_bound_utc = window.open_utc
                if normalized_mode == "incremental":
                    cursor_utc = await _read_cursor_utc(
                        session,
                        symbol=symbol,
                        exchange=exchange,
                    )
                    lower_bound_utc = _compute_incremental_lower_bound(
                        cursor_utc,
                        window.open_utc,
                    )

                if window.last_minute_utc < lower_bound_utc:
                    continue

                rows, page_calls = await _collect_window_rows(
                    kis=kis,
                    symbol=symbol,
                    exchange=exchange,
                    lower_bound_utc=lower_bound_utc,
                    upper_bound_utc=window.last_minute_utc,
                )
                pair_rows.extend(rows)
                pair_pages += page_calls

            if not pair_rows and pair_pages == 0:
                pairs_skipped += 1
                continue

            try:
                rows_upserted += await _upsert_rows(session, pair_rows)
                await session.commit()
            except Exception:
                await session.rollback()
                raise

            pairs_processed += 1
            pages_fetched += pair_pages

        return {
            "mode": normalized_mode,
            "sessions": session_count,
            "skipped": pairs_processed == 0,
            "skip_reasons": {},
            "symbols_total": len(target_symbols),
            "symbol_venues_total": pairs_total,
            "pairs_processed": pairs_processed,
            "pairs_skipped": pairs_skipped,
            "rows_upserted": rows_upserted,
            "pages_fetched": pages_fetched,
        }
    finally:
        await session.close()


__all__ = ["sync_us_candles"]
