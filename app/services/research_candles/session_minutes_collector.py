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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
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
from app.services.brokers.kis.live_order_expiry import (
    KR_PREMARKET_OPEN_KST,
    KR_REGULAR_OPEN_KST,
)
from app.services.research_candles.session_segment import (
    session_segment_of,
    us_session_date,
)

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
    value: Decimal | None  # KR: NULL when the per-minute value cannot be proven


@dataclass(slots=True)
class FrameResult:
    bars: list[MinuteBar] = field(default_factory=list)
    skipped_forming: int = 0
    skipped_invalid: int = 0
    null_value: int = 0  # KR bars kept with a NULL value


#: First minute of a KR venue's day: its accumulated value starts from zero, so
#: the accumulated value itself is that bar's value.
_KR_DAY_FIRST_MINUTE = {"KRX": KR_REGULAR_OPEN_KST, "NTX": KR_PREMARKET_OPEN_KST}


def is_closed(bar_start_utc: datetime, observed_before: datetime) -> bool:
    """True once the minute is over by at least the 10 s grace."""
    return bar_start_utc + BAR + CLOSE_GRACE <= observed_before


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
    """KIS frame (``datetime`` = naive START label in the local clock) -> bars.

    KR ``value`` (``acml_tr_pbmn``) is the ACCUMULATED trade value of the day,
    not the per-minute amount (``volume`` is per minute). The per-minute value is
    stored only when it is proven:

    * the previous row of the same KST day is in the frame -> the difference, or
    * the bar is exactly the venue's first minute of the day (KRX 09:00,
      NTX 08:00) -> the accumulated value itself.

    Anything else is stored as NULL — the bar is kept, the value is not guessed
    (unknown is neither 0 nor an estimate; frame length is never used to infer
    that the day start was reached). A row whose accumulated value went down, the
    row after it, and the row after a row dropped as invalid are NULL too, because
    the baseline (or a dropped minute's trade value) would otherwise be folded
    into the difference. US ``value`` is per bar and stored as is.
    """
    result = FrameResult()
    if frame is None or frame.empty:
        return result
    local_tz = _KST if market == "kr" else _ET
    parsed_rows: dict[datetime, list[Decimal]] = {}
    invalid_starts: list[datetime] = []
    records = frame.to_dict("records")
    for item in records:
        parsed = pd.to_datetime(item.get("datetime"), errors="coerce")
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
        if start_utc in parsed_rows:
            continue
        values = [
            _decimal(item.get(key))
            for key in ("open", "high", "low", "close", "volume", "value")
        ]
        if any(v is None for v in values[:5]) or (values[5] is None and market != "kr"):
            result.skipped_invalid += 1
            invalid_starts.append(start_utc)
            continue
        if values[5] is None:
            # KR bar is usable, its accumulated value is not: acts as a chain break
            invalid_starts.append(start_utc)
        parsed_rows[start_utc] = values  # type: ignore[assignment]

    previous: tuple[datetime, Decimal] | None = None
    suspect_next = False
    for start_utc in sorted(parsed_rows):
        o, h, low, c, vol, value = parsed_rows[start_utc]
        if market == "kr":
            local = start_utc.astimezone(_KST)
            accumulated, value = value, None
            same_day_previous = (
                previous is not None
                and previous[0].astimezone(_KST).date() == local.date()
            )
            lower = previous[0] if same_day_previous and previous else None
            dropped_between = any(
                dropped < start_utc
                and dropped.astimezone(_KST).date() == local.date()
                and (lower is None or dropped > lower)
                for dropped in invalid_starts
            )
            if not same_day_previous:
                suspect_next = False
            decreased = False
            if not dropped_between and not suspect_next and accumulated is not None:
                if same_day_previous and previous is not None:
                    if accumulated >= previous[1]:
                        value = accumulated - previous[1]
                    else:
                        decreased = True
                elif local.time() == _KR_DAY_FIRST_MINUTE.get(venue):
                    value = accumulated
            suspect_next = decreased
            previous = (start_utc, accumulated) if accumulated is not None else previous
            if value is None and is_closed(start_utc, observed_before):
                result.null_value += 1
        if not is_closed(start_utc, observed_before):
            result.skipped_forming += 1
            continue
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
                value=value,
            )
        )
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


#: Highest priority first. holdings > fixed list > snapshot rank > pick log (hk 6145).
TARGET_ORIGIN_ORDER = (
    "kis_holdings",
    "manual_holdings",
    "fixed_list",
    "screener_snapshot",
    "screener_pick_log",
)
DEFAULT_SCREENER_TOP_N = 20
DEFAULT_MAX_SYMBOLS = 150


@dataclass(slots=True)
class TargetSet:
    symbols: list[str]
    by_origin: dict[str, int]
    errors: list[dict[str, str]] = field(default_factory=list)
    truncated: dict[str, int] = field(default_factory=dict)


def select_with_cap(
    tiers: Sequence[tuple[str, Sequence[str]]], max_symbols: int
) -> tuple[list[str], dict[str, int]]:
    """Keep symbols in tier order up to ``max_symbols``; report what was cut.

    A symbol found in several tiers belongs to the first (highest priority) one.
    Returns ``(kept_in_priority_order, truncated_count_by_origin)``.
    """
    ordered: list[tuple[str, str]] = []
    seen: set[str] = set()
    for origin, symbols in tiers:
        for symbol in symbols:
            if symbol not in seen:
                seen.add(symbol)
                ordered.append((origin, symbol))
    cap = max(int(max_symbols), 0)
    kept = [symbol for _, symbol in ordered[:cap]]
    truncated: dict[str, int] = {}
    for origin, _ in ordered[cap:]:
        truncated[origin] = truncated.get(origin, 0) + 1
    return kept, truncated


def _row_rank(row: Mapping[str, Any], position: int) -> int:
    rank = row.get("rank")
    return (
        rank
        if isinstance(rank, int) and not isinstance(rank, bool) and rank >= 1
        else position
    )


async def snapshot_top_symbols(
    *,
    market: Market,
    top_n: int,
    snapshot_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    presets_fn: Callable[[str], Sequence[Any]] | None = None,
) -> tuple[list[str], list[dict[str, str]]]:
    """Union of the top ``top_n`` rows of every available screener preset.

    DB-only: ``screen_stocks_snapshot_impl`` makes no HTTP call and — being the
    impl, not the registered MCP tool — never writes ``screener_pick_log``
    (hard rule 13: the observer is wired in the registration layer only). No
    observer sink argument is passed. Symbols are ordered by best rank across
    presets, then by preset order.
    """
    if snapshot_fn is None:
        from app.mcp_server.tooling.screener_snapshot_tool import (
            screen_stocks_snapshot_impl as snapshot_fn,
        )
    if presets_fn is None:
        from app.services.invest_view_model.screener_presets import (
            preset_definitions as presets_fn,
        )

    best: dict[str, tuple[int, int]] = {}
    errors: list[dict[str, str]] = []
    limit = max(int(top_n), 1)
    available = [
        p for p in presets_fn(market) if getattr(p, "availability", None) == "active"
    ]
    for preset_index, preset in enumerate(available):
        preset_id = str(getattr(preset, "id", ""))
        try:
            payload = await snapshot_fn(preset=preset_id, market=market, limit=limit)
        except Exception as exc:  # noqa: BLE001 - one preset must not stop target selection
            errors.append(
                {
                    "origin": "screener_snapshot",
                    "preset": preset_id,
                    "error_class": type(exc).__name__,
                }
            )
            continue
        if isinstance(payload, Mapping) and payload.get("error"):
            errors.append(
                {
                    "origin": "screener_snapshot",
                    "preset": preset_id,
                    "error_class": "SnapshotError",
                }
            )
            continue
        rows = (payload or {}).get("results") or []
        for position, row in enumerate(rows[:limit], start=1):
            symbol = normalize_symbol(market, row.get("symbol"))
            if symbol is None:
                continue
            key = (_row_rank(row, position), preset_index)
            if symbol not in best or key < best[symbol]:
                best[symbol] = key
    ordered = [
        symbol for symbol, _ in sorted(best.items(), key=lambda kv: (kv[1], kv[0]))
    ]
    return ordered, errors


async def build_targets(
    *,
    market: Market,
    session: AsyncSession,
    kis: KisMinuteReader,
    user_id: int,
    fixed_symbols_raw: str | None,
    now: datetime,
    screener_top_n: int = DEFAULT_SCREENER_TOP_N,
    max_symbols: int = DEFAULT_MAX_SYMBOLS,
    snapshot_fn: Callable[..., Awaitable[dict[str, Any]]] | None = None,
    presets_fn: Callable[[str], Sequence[Any]] | None = None,
) -> TargetSet:
    """Holdings (KIS + manual) > fixed list > screener snapshot top N > pick log.

    Over ``max_symbols`` the lower tiers are cut first and the number cut per
    origin is reported in ``truncated``.
    """
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    found: dict[str, list[str]] = {}
    errors: list[dict[str, str]] = []

    def add(origin: str, values: Iterable[object]) -> None:
        symbols = (normalize_symbol(market, value) for value in values)
        found[origin] = sorted({s for s in symbols if s})

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

    found["fixed_list"] = parse_symbol_list(market, fixed_symbols_raw)

    try:
        ranked, snapshot_errors = await snapshot_top_symbols(
            market=market,
            top_n=screener_top_n,
            snapshot_fn=snapshot_fn,
            presets_fn=presets_fn,
        )
        found["screener_snapshot"] = ranked
        errors.extend(snapshot_errors)
    except Exception as exc:  # noqa: BLE001
        errors.append(
            {"origin": "screener_snapshot", "error_class": type(exc).__name__}
        )

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

    tiers = [(origin, found.get(origin, [])) for origin in TARGET_ORIGIN_ORDER]
    symbols, truncated = select_with_cap(tiers, max_symbols)
    return TargetSet(
        symbols=sorted(symbols),
        by_origin={origin: len(values) for origin, values in found.items() if values},
        errors=errors,
        truncated=truncated,
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
    inserted = already_present = skipped_forming = skipped_invalid = 0
    null_value = would_write = 0
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
            null_value += parsed.null_value
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
        "null_value": null_value,
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
    "select_with_cap",
    "snapshot_top_symbols",
    "bars_from_frame",
    "build_targets",
    "collect",
    "is_closed",
    "load_nxt_eligible",
    "new_batch_id",
    "normalize_symbol",
    "parse_symbol_list",
    "resolve_us_exchanges",
]
