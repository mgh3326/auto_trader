"""hk 6145: closed 1-minute bars -> research minute tables, insert-only.

KR (venues KRX via KIS market ``J`` and NTX via ``NX``) goes to
``research.kr_candles_1m``; US (KIS overseas minute chart, 04:00-20:00 ET) goes
to ``research.us_candles_1m``. ``public.*_candles_1m`` are never written.

Rules (design hk:doc design/2026-11-24/at-session-timeline §4):

* KIS labels a minute bar by its START in the exchange's local clock (KST for
  KR, ET for US) and includes the still-forming current minute. A bar is kept
  only when ``bar_start + 1 min + 10 s <= observed_before`` where
  ``observed_before`` is read BEFORE the request goes out, so the stored bar
  can never be a partial one.
* ``retrieved_at`` is the first receipt: rows are ``INSERT ... ON CONFLICT DO
  NOTHING``; a re-run never touches an existing row, so its ``retrieved_at``
  stays.
* ``session_segment`` comes from ``session_segment_of`` (bar start, venue).
  An unknown segment is stored as ``UNKNOWN``, never guessed.
* No backfill, no scheduler: this module registers nothing.

The KIS client is injected and only four read methods are used (see
``KisMinuteReader``); this module imports no order, ledger, approval or
broker-mutation code.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import pandas as pd
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.core.timezone import kst_day_window
from app.services.execution_ledger.kis_lots import us_trading_day_window
from app.services.research_candles.session_segment import session_segment_of

logger = logging.getLogger(__name__)

Market = Literal["kr", "us"]

SOURCE = "KIS"
BAR = timedelta(minutes=1)
CLOSE_GRACE = timedelta(seconds=10)
BATCH_PREFIX = "session-minutes"

_KST = ZoneInfo("Asia/Seoul")
_ET = ZoneInfo("America/New_York")

KR_TABLE = "research.kr_candles_1m"
US_TABLE = "research.us_candles_1m"
#: KIS market codes for the two KR venues.
KR_VENUE_MARKET_CODE = {"KRX": "J", "NTX": "NX"}
US_PICK_LOOKBACK = timedelta(hours=24)
_US_VENUES = frozenset({"NASD", "NYSE", "AMEX"})


class KisMinuteReader(Protocol):
    """The only KIS surface the collector may touch (all read-only)."""

    async def inquire_minute_chart(
        self, code: str, market: str = "J", time_unit: int = 1, n: int = 200
    ) -> pd.DataFrame: ...

    async def inquire_overseas_minute_chart(
        self, symbol: str, exchange_code: str = "NASD", n: int = 120, keyb: str = ""
    ) -> Any: ...

    async def fetch_my_stocks(self) -> list[dict[str, Any]]: ...

    async def fetch_my_us_stocks(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class MinuteBar:
    time_utc: datetime  # bar START
    session_date: date  # KST date (kr) / ET trading day (us)
    symbol: str
    venue: str
    session_segment: str
    source: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    value: Decimal


@dataclass(slots=True)
class FrameResult:
    bars: list[MinuteBar] = field(default_factory=list)
    skipped_forming: int = 0
    skipped_invalid: int = 0


def is_closed(bar_start_utc: datetime, observed_before: datetime) -> bool:
    """True once the minute is over by at least the 10 s grace."""
    return bar_start_utc + BAR + CLOSE_GRACE <= observed_before


def us_session_date(bar_start_utc: datetime) -> date:
    """ET trading day of ``bar_start_utc`` (20:00 ET rollover, kis_lots helper)."""
    return us_trading_day_window(bar_start_utc)[1].astimezone(_ET).date()


def _decimal(value: object) -> Decimal | None:
    try:
        if value is None or pd.isna(value):
            return None
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def bars_from_frame(
    frame: pd.DataFrame,
    *,
    market: Market,
    symbol: str,
    venue: str,
    observed_before: datetime,
) -> FrameResult:
    """KIS frame (``datetime`` = naive START label in the local clock) -> bars."""
    result = FrameResult()
    if frame is None or frame.empty:
        return result
    local_tz = _KST if market == "kr" else _ET
    seen: set[datetime] = set()
    for item in frame.to_dict("records"):
        raw = item.get("datetime")
        parsed = pd.to_datetime(raw, errors="coerce")
        if pd.isna(parsed):
            result.skipped_invalid += 1
            continue
        stamp = pd.Timestamp(parsed)
        stamp = (
            stamp.tz_localize(local_tz)
            if stamp.tzinfo is None
            else stamp.tz_convert(local_tz)
        )
        start_utc = (
            stamp.to_pydatetime().astimezone(UTC).replace(second=0, microsecond=0)
        )
        if start_utc in seen:
            continue
        values = [
            _decimal(item.get(key))
            for key in ("open", "high", "low", "close", "volume", "value")
        ]
        if any(v is None for v in values):
            result.skipped_invalid += 1
            continue
        if not is_closed(start_utc, observed_before):
            result.skipped_forming += 1
            continue
        seen.add(start_utc)
        o, h, low, c, vol, val = values  # type: ignore[misc]
        result.bars.append(
            MinuteBar(
                time_utc=start_utc,
                session_date=(
                    start_utc.astimezone(_KST).date()
                    if market == "kr"
                    else us_session_date(start_utc)
                ),
                symbol=symbol,
                venue=venue,
                session_segment=session_segment_of(market, venue, start_utc),
                source=SOURCE,
                open=o,
                high=h,
                low=low,
                close=c,
                volume=vol,
                value=val,
            )
        )
    result.bars.sort(key=lambda bar: bar.time_utc)
    return result


# --------------------------------------------------------------------- writer


class MinuteWriter(Protocol):
    async def insert(
        self,
        bars: Sequence[MinuteBar],
        *,
        batch_id: str,
        retrieved_at: datetime,
    ) -> tuple[int, int]:
        """Return ``(inserted, already_present)``."""
        ...


_KR_INSERT = text(
    f"""
    INSERT INTO {KR_TABLE}
        (time_utc, session_date_kst, symbol, venue, session_segment, source,
         open, high, low, close, volume, value, retrieved_at, batch_id)
    VALUES
        (:time_utc, :session_date, :symbol, :venue, :session_segment, :source,
         :open, :high, :low, :close, :volume, :value, :retrieved_at, :batch_id)
    ON CONFLICT (time_utc, symbol, venue) DO NOTHING
    """
)
_US_INSERT = text(
    f"""
    INSERT INTO {US_TABLE}
        (time_utc, session_date, symbol, venue, session_segment, source,
         open, high, low, close, volume, value, retrieved_at, batch_id)
    VALUES
        (:time_utc, :session_date, :symbol, :venue, :session_segment, :source,
         :open, :high, :low, :close, :volume, :value, :retrieved_at, :batch_id)
    ON CONFLICT (time_utc, symbol, venue) DO NOTHING
    """
)


class SqlMinuteWriter:
    """Insert-only writer; commits once per call (one symbol x venue)."""

    def __init__(self, session: AsyncSession, market: Market) -> None:
        self._session = session
        self._statement = _KR_INSERT if market == "kr" else _US_INSERT

    async def insert(
        self,
        bars: Sequence[MinuteBar],
        *,
        batch_id: str,
        retrieved_at: datetime,
    ) -> tuple[int, int]:
        inserted = 0
        try:
            for bar in bars:
                outcome = await self._session.execute(
                    self._statement,
                    {
                        "time_utc": bar.time_utc,
                        "session_date": bar.session_date,
                        "symbol": bar.symbol,
                        "venue": bar.venue,
                        "session_segment": bar.session_segment,
                        "source": bar.source,
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "volume": bar.volume,
                        "value": bar.value,
                        "retrieved_at": retrieved_at,
                        "batch_id": batch_id,
                    },
                )
                inserted += 1 if (getattr(outcome, "rowcount", 0) or 0) > 0 else 0
            await self._session.commit()
        except Exception:
            await self._session.rollback()
            raise
        return inserted, len(bars) - inserted


# -------------------------------------------------------------------- targets


def normalize_symbol(market: Market, value: object) -> str | None:
    raw = str(value or "").strip().upper()
    if not raw:
        return None
    if market == "kr":
        raw = raw.zfill(6) if len(raw) < 6 else raw
        return raw if len(raw) == 6 and raw.isalnum() else None
    return to_db_symbol(raw) or None


def parse_symbol_list(market: Market, raw: str | None) -> list[str]:
    symbols = (normalize_symbol(market, part) for part in (raw or "").split(","))
    return sorted({s for s in symbols if s})


@dataclass(slots=True)
class TargetSet:
    symbols: list[str]
    by_origin: dict[str, int]
    errors: list[dict[str, str]] = field(default_factory=list)


async def build_targets(
    *,
    market: Market,
    session: AsyncSession,
    kis: KisMinuteReader,
    user_id: int,
    fixed_symbols_raw: str | None,
    now: datetime,
) -> TargetSet:
    """KIS holdings + manual holdings + today's admitted picks + fixed list."""
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    found: dict[str, set[str]] = {}
    errors: list[dict[str, str]] = []

    def add(origin: str, values: Iterable[object]) -> None:
        for value in values:
            symbol = normalize_symbol(market, value)
            if symbol:
                found.setdefault(origin, set()).add(symbol)

    try:
        if market == "kr":
            holdings = await kis.fetch_my_stocks()
            add("kis_holdings", (item.get("pdno") for item in holdings))
        else:
            holdings = await kis.fetch_my_us_stocks()
            add("kis_holdings", (item.get("ovrs_pdno") for item in holdings))
    except Exception as exc:  # noqa: BLE001 - other origins still count
        errors.append({"origin": "kis_holdings", "error_class": type(exc).__name__})

    try:
        manual = await ManualHoldingsService(session).get_holdings_by_user(
            user_id=user_id,
            market_type=MarketType.KR if market == "kr" else MarketType.US,
        )
        add("manual_holdings", (getattr(h, "ticker", None) for h in manual))
    except Exception as exc:  # noqa: BLE001
        errors.append({"origin": "manual_holdings", "error_class": type(exc).__name__})

    try:
        if market == "kr":
            start, end = kst_day_window(now)
        else:
            start, end = now - US_PICK_LOOKBACK, now + timedelta(minutes=1)
        rows = await session.execute(
            text(
                "SELECT DISTINCT symbol FROM review.screener_pick_log "
                "WHERE market = :market AND admission = 'admitted' "
                "AND recorded_at >= :start AND recorded_at < :end"
            ),
            {"market": market, "start": start, "end": end},
        )
        add("screener_pick_log", rows.scalars().all())
    except Exception as exc:  # noqa: BLE001
        await session.rollback()
        errors.append(
            {"origin": "screener_pick_log", "error_class": type(exc).__name__}
        )

    add("fixed_list", parse_symbol_list(market, fixed_symbols_raw))
    symbols = sorted(set().union(*found.values())) if found else []
    return TargetSet(
        symbols=symbols,
        by_origin={origin: len(values) for origin, values in sorted(found.items())},
        errors=errors,
    )


async def load_nxt_eligible(session: AsyncSession, symbols: Sequence[str]) -> set[str]:
    if not symbols:
        return set()
    rows = await session.execute(
        text(
            "SELECT symbol FROM public.kr_symbol_universe "
            "WHERE symbol = ANY(:symbols) AND nxt_eligible IS TRUE"
        ),
        {"symbols": list(symbols)},
    )
    return {str(value) for value in rows.scalars().all()}


async def resolve_us_exchanges(
    session: AsyncSession, symbols: Sequence[str]
) -> tuple[dict[str, str], list[str]]:
    from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

    resolved: dict[str, str] = {}
    unresolved: list[str] = []
    for symbol in symbols:
        try:
            exchange = str(await get_us_exchange_by_symbol(symbol, db=session))
        except Exception:  # noqa: BLE001 - reported as an error row, run continues
            await session.rollback()
            unresolved.append(symbol)
            continue
        if exchange in _US_VENUES:
            resolved[symbol] = exchange
        else:
            unresolved.append(symbol)
    return resolved, unresolved


# ------------------------------------------------------------------------ run


def new_batch_id(market: Market, now: datetime) -> str:
    return f"{BATCH_PREFIX}-{market}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


async def collect(
    *,
    market: Market,
    symbols: Sequence[str],
    kis: KisMinuteReader,
    writer: MinuteWriter | None,
    nxt_eligible: Iterable[str] = (),
    us_exchanges: Mapping[str, str] | None = None,
    unresolved_symbols: Sequence[str] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    batch_id: str | None = None,
    dry_run: bool = False,
    prior_errors: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    """Read, filter to closed bars, write. ``writer`` is unused when ``dry_run``."""
    batch = batch_id or new_batch_id(market, clock())
    nxt = set(nxt_eligible)
    exchanges = us_exchanges or {}
    inserted = already_present = skipped_forming = skipped_invalid = would_write = 0
    reads = 0
    errors: list[dict[str, str]] = [dict(item) for item in prior_errors]
    for symbol in unresolved_symbols:
        errors.append({"symbol": symbol, "error_class": "UnresolvedSymbol"})

    plan: list[tuple[str, str]] = []  # (symbol, venue)
    for symbol in symbols:
        if market == "kr":
            plan.append((symbol, "KRX"))
            if symbol in nxt:
                plan.append((symbol, "NTX"))
        elif symbol in exchanges:
            plan.append((symbol, exchanges[symbol]))

    for symbol, venue in plan:
        observed_before = clock()
        try:
            if market == "kr":
                frame = await kis.inquire_minute_chart(
                    symbol, market=KR_VENUE_MARKET_CODE[venue], time_unit=1, n=200
                )
            else:
                page = await kis.inquire_overseas_minute_chart(
                    symbol, exchange_code=venue, n=120
                )
                frame = page.frame
            reads += 1
            parsed = bars_from_frame(
                frame,
                market=market,
                symbol=symbol,
                venue=venue,
                observed_before=observed_before,
            )
            skipped_forming += parsed.skipped_forming
            skipped_invalid += parsed.skipped_invalid
            if dry_run:
                would_write += len(parsed.bars)
            elif parsed.bars:
                assert writer is not None
                new, present = await writer.insert(
                    parsed.bars, batch_id=batch, retrieved_at=clock()
                )
                inserted += new
                already_present += present
        except Exception as exc:  # noqa: BLE001 - one symbol x venue must not stop the run
            logger.warning(
                "session minutes read/write failed symbol=%s venue=%s error=%s",
                symbol,
                venue,
                type(exc).__name__,
            )
            errors.append(
                {"symbol": symbol, "venue": venue, "error_class": type(exc).__name__}
            )

    return {
        "market": market,
        "batch_id": batch,
        "dry_run": dry_run,
        "symbols": len(symbols),
        "reads": reads,
        "inserted": inserted,
        "already_present": already_present,
        "skipped_forming": skipped_forming,
        "skipped_invalid": skipped_invalid,
        "would_write": would_write if dry_run else None,
        "errors": len(errors),
        "error_details": errors,
    }


__all__ = [
    "KisMinuteReader",
    "MinuteBar",
    "MinuteWriter",
    "SqlMinuteWriter",
    "TargetSet",
    "bars_from_frame",
    "build_targets",
    "collect",
    "is_closed",
    "load_nxt_eligible",
    "new_batch_id",
    "normalize_symbol",
    "parse_symbol_list",
    "resolve_us_exchanges",
    "us_session_date",
]
