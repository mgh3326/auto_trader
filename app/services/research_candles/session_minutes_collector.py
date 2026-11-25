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
        self,
        code: str,
        market: str = "J",
        time_unit: int = 1,
        n: int = 200,
        end_time: str | None = None,
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


#: Origin order of the holdings / fixed / movers / screener / pick-log candidates
#: (report key order only; selection is by tier, see ``TIER_ORDER``).
TARGET_ORIGIN_ORDER = (
    "kis_holdings",
    "manual_holdings",
    "fixed_list",
    "intraday_movers",
    "screener_snapshot",
    "screener_pick_log",
)
#: Selection tiers, highest priority first (hk 6299). ``fixed`` = holdings (KIS and
#: manual) + the fixed list: never cut. ``intraday_movers`` = KR intraday promotion
#: (today's 6154 first captures). ``screener_snapshot`` = pre-open screen top N.
#: ``screener_pick_log`` = the remainder.
TIER_FIXED = "fixed"
TIER_ORDER = (TIER_FIXED, "intraday_movers", "screener_snapshot", "screener_pick_log")
CUT_REASON_BUDGET = "budget_cut"
DEFAULT_SCREENER_TOP_N = 20
DEFAULT_MAX_SYMBOLS = 150
DEFAULT_FIXED_BUDGET = 50
DEFAULT_INTRADAY_BUDGET = 40
DEFAULT_SCREENER_BUDGET = 60


@dataclass(frozen=True, slots=True)
class TierSpec:
    """One selection tier. ``symbols`` are in priority order (may repeat).

    ``budget`` None = no per-tier budget (the run cap still applies). ``exempt``
    tiers are never cut; their unused budget still spills to the next tier.
    ``protected`` symbols are kept ahead of the tier budget (not ahead of the run
    cap). Only protected symbols that are still candidates of this tier count, so a
    symbol a higher tier already took cannot raise this tier's allowance.
    """

    tier: str
    symbols: Sequence[str]
    budget: int | None
    exempt: bool = False
    protected: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class TierReport:
    tier: str
    budget: int | None  # base budget
    allowance: (
        int | None
    )  # base + spill-over, before the run cap; None = unbudgeted/exempt
    candidates: int  # not already taken by a higher tier
    selected: int
    cut: int


@dataclass(frozen=True, slots=True)
class TieredSelection:
    selected: list[str]  # in priority order
    tiers: list[TierReport]
    cuts: list[dict[str, str]]  # {"symbol", "tier", "reason"} for symbols left out
    truncated: dict[str, int]  # cut count per tier


def select_tiered(tiers: Sequence[TierSpec], max_symbols: int) -> TieredSelection:
    """Pick symbols tier by tier under a per-tier budget and one run cap.

    Unused budget of a tier (``budget + carry - taken``) carries to the next tier.
    A symbol that a tier does not take stays eligible for the lower tiers and is
    reported as cut only when no tier takes it; the cut reason is the first tier
    that left it out. Exempt tiers consume the run cap but are never trimmed by it.
    """
    remaining = max(int(max_symbols), 0)
    carry = 0
    chosen: list[str] = []
    chosen_set: set[str] = set()
    cut_by: dict[str, str] = {}
    cut_order: list[str] = []
    reports: list[tuple[TierSpec, list[str], list[str], int | None]] = []
    for spec in tiers:
        listed: set[str] = set()
        open_symbols: list[str] = []
        for symbol in spec.symbols:
            if symbol not in chosen_set and symbol not in listed:
                listed.add(symbol)
                open_symbols.append(symbol)
        # protected candidates first (stable), so the prefix slice keeps them
        candidates = [s for s in open_symbols if s in spec.protected] + [
            s for s in open_symbols if s not in spec.protected
        ]
        kept_count = sum(1 for s in candidates if s in spec.protected)
        pool = None if spec.budget is None else spec.budget + carry
        if spec.exempt:
            take = candidates
        else:
            limit = remaining if pool is None else min(max(pool, kept_count), remaining)
            take = candidates[:limit]
        taken = set(take)
        cut = [symbol for symbol in candidates if symbol not in taken]
        for symbol in cut:
            if symbol not in cut_by:
                cut_by[symbol] = spec.tier
                cut_order.append(symbol)
        chosen.extend(take)
        chosen_set.update(take)
        remaining = max(remaining - len(take), 0)
        carry = 0 if pool is None else max(pool - len(take), 0)
        reports.append((spec, candidates, take, pool))

    cuts = [
        {"symbol": symbol, "tier": cut_by[symbol], "reason": CUT_REASON_BUDGET}
        for symbol in cut_order
        if symbol not in chosen_set
    ]
    cut_counts: dict[str, int] = {}
    for item in cuts:
        cut_counts[item["tier"]] = cut_counts.get(item["tier"], 0) + 1
    tier_reports = [
        TierReport(
            tier=spec.tier,
            budget=spec.budget,
            allowance=None if (spec.exempt or pool is None) else pool,
            candidates=len(candidates),
            selected=len(take),
            cut=cut_counts.get(spec.tier, 0),
        )
        for spec, candidates, take, pool in reports
    ]
    return TieredSelection(
        selected=chosen,
        tiers=tier_reports,
        cuts=cuts,
        truncated={k: v for k, v in cut_counts.items() if v},
    )


def select_with_cap(
    tiers: Sequence[tuple[str, Sequence[str]]], max_symbols: int
) -> tuple[list[str], dict[str, int]]:
    """Cap-only selection in the given order (no per-tier budgets).

    Returns ``(kept_in_priority_order, truncated_count_by_origin)``.
    """
    selection = select_tiered(
        [TierSpec(origin, symbols, budget=None) for origin, symbols in tiers],
        max_symbols,
    )
    return selection.selected, selection.truncated


@dataclass(slots=True)
class TargetSet:
    symbols: list[str]
    by_origin: dict[str, int]
    errors: list[dict[str, str]] = field(default_factory=list)
    truncated: dict[str, int] = field(default_factory=dict)
    #: KR symbols first seen as intraday movers that have no minute row yet today:
    #: they get one extra, earlier KIS page (hk 6154). Subset of ``symbols``.
    backfill: list[str] = field(default_factory=list)
    tiers: list[TierReport] = field(default_factory=list)
    cuts: list[dict[str, str]] = field(default_factory=list)


def targets_report(targets: TargetSet) -> dict[str, Any]:
    """The per-run target fields of the collector JSON (hk 6299)."""
    return {
        "targeted_symbols": list(targets.symbols),
        "targets_by_origin": dict(targets.by_origin),
        "targets_truncated": dict(targets.truncated),
        "targets_truncated_total": sum(targets.truncated.values()),
        "targets_tiers": [
            {
                "tier": t.tier,
                "budget": t.budget,
                "allowance": t.allowance,
                "candidates": t.candidates,
                "selected": t.selected,
                "cut": t.cut,
            }
            for t in targets.tiers
        ],
        "targets_cuts": [dict(c) for c in targets.cuts],
    }


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
    fixed_budget: int = DEFAULT_FIXED_BUDGET,
    intraday_budget: int = DEFAULT_INTRADAY_BUDGET,
    screener_budget: int = DEFAULT_SCREENER_BUDGET,
) -> TargetSet:
    """Tiered selection under ``max_symbols`` (hk 6299).

    1. ``fixed``: KIS + manual holdings and the fixed list. Never cut; its unused
       part of ``fixed_budget`` spills to the next tier.
    2. ``intraday_movers`` (KR only): today's first captures (hk 6154), ordered by
       earliest capture then best rank; ``intraday_budget``. A symbol that already
       has minute rows today is protected from the budget (never demoted the same
       day).
    3. ``screener_snapshot``: top ``screener_top_n`` of every active preset, by best
       rank; ``screener_budget``.
    4. ``screener_pick_log``: today's admitted symbols, the remainder of the cap.

    Symbols a tier does not take remain eligible for the lower tiers; the ones
    nobody takes are reported in ``cuts`` with their tier and reason. ``truncated``
    keeps the cut count per tier.
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

    mover_order: list[str] = []
    protected: set[str] = set()
    if market == "kr":
        try:
            from app.services.invest_momentum_events.intraday import first_captures

            captures = await first_captures(
                session, now.astimezone(_KST).date(), market="kr"
            )
            # Promotion order: earliest capture first, then best rank, then symbol.
            ranked = sorted(
                {c.symbol: c for c in captures}.values(),
                key=lambda c: (c.snapshot_at, c.rank, c.symbol),
            )
            for capture in ranked:
                symbol = normalize_symbol("kr", capture.symbol)
                if symbol and symbol not in mover_order:
                    mover_order.append(symbol)
            found["intraday_movers"] = list(mover_order)
        except Exception as exc:  # noqa: BLE001 - other origins still count
            await session.rollback()
            errors.append(
                {"origin": "intraday_movers", "error_class": type(exc).__name__}
            )
    have_today: set[str] = set()
    have_known = False
    if mover_order:
        try:
            have_today = await load_symbols_with_rows_today(session, mover_order, now)
            have_known = True
        except Exception as exc:  # noqa: BLE001 - no protection, the run continues
            await session.rollback()
            errors.append(
                {"origin": "intraday_movers", "error_class": type(exc).__name__}
            )
        protected = {s for s in mover_order if s in have_today}
    mover_candidates = [s for s in mover_order if s in protected] + [
        s for s in mover_order if s not in protected
    ]

    fixed_tier = [
        *found.get("kis_holdings", []),
        *found.get("manual_holdings", []),
        *found.get("fixed_list", []),
    ]
    specs = [
        TierSpec(TIER_FIXED, fixed_tier, budget=fixed_budget, exempt=True),
    ]
    if market == "kr":
        specs.append(
            TierSpec(
                "intraday_movers",
                mover_candidates,
                budget=intraday_budget,
                protected=frozenset(protected),
            )
        )
    specs.append(
        TierSpec(
            "screener_snapshot",
            found.get("screener_snapshot", []),
            budget=screener_budget,
        )
    )
    specs.append(
        TierSpec("screener_pick_log", found.get("screener_pick_log", []), budget=None)
    )
    selection = select_tiered(specs, max_symbols)

    kept = set(selection.selected)
    backfill: list[str] = []
    if have_known:
        # Kept movers with no minute row yet today get the earlier KIS page (hk 6154).
        backfill = sorted(s for s in mover_order if s in kept and s not in have_today)
    return TargetSet(
        symbols=sorted(selection.selected),
        by_origin={origin: len(values) for origin, values in found.items() if values},
        errors=errors,
        truncated=selection.truncated,
        backfill=backfill,
        tiers=selection.tiers,
        cuts=selection.cuts,
    )


async def load_symbols_with_rows_today(
    session: AsyncSession, symbols: Sequence[str], now: datetime
) -> set[str]:
    """KR symbols that already have at least one minute row for today's KST date."""
    if not symbols:
        return set()
    rows = await session.execute(
        text(
            f"SELECT DISTINCT symbol FROM {KR_TABLE} "
            "WHERE symbol = ANY(:symbols) AND session_date_kst = :day"
        ),
        {"symbols": list(symbols), "day": now.astimezone(_KST).date()},
    )
    return {str(value) for value in rows.scalars().all()}


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


async def _with_earlier_page(
    kis: KisMinuteReader, frame: pd.DataFrame, *, symbol: str, venue: str
) -> tuple[pd.DataFrame, int]:
    """Append ONE earlier KIS page to ``frame`` for a newly seen mover (hk 6154).

    KIS returns about 30 bars per call whatever ``n`` is, so a symbol first seen
    mid-session would only get the last ~30 minutes. The earlier page is read
    with the hour cursor (``FID_INPUT_HOUR_1`` = the earliest bar's time in the
    first page); an overlapping bar is harmless (same key, ``DO NOTHING``).
    Nothing is requested when the first page is empty or already starts at the
    venue's first minute. A failing earlier read raises; the caller keeps the
    first page and reports the error, so the newest page is never lost.

    Returns ``(combined_frame, pages_read)``.
    """
    if frame is None or frame.empty:
        return frame, 0
    stamps = pd.to_datetime(frame["datetime"], errors="coerce").dropna()
    if stamps.empty:
        return frame, 0
    earliest = stamps.min()
    first_minute = _KR_DAY_FIRST_MINUTE.get(venue)
    if first_minute is not None and earliest.time() <= first_minute:
        return frame, 0
    older = await kis.inquire_minute_chart(
        symbol,
        market=KR_VENUE_MARKET_CODE[venue],
        time_unit=1,
        n=200,
        end_time=earliest.strftime("%H%M%S"),
    )
    if older is None or older.empty:
        return frame, 1
    combined = pd.concat([older, frame], ignore_index=True)
    combined = combined.drop_duplicates(subset=["datetime"], keep="last")
    return combined.sort_values("datetime").reset_index(drop=True), 1


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
    backfill_symbols: Iterable[str] = (),
) -> dict[str, Any]:
    """Read, filter to closed bars, write. ``writer`` is unused when ``dry_run``."""
    batch = batch_id or new_batch_id(market, clock())
    nxt = set(nxt_eligible)
    backfill = set(backfill_symbols) if market == "kr" else set()
    backfill_pages = 0
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
                if symbol in backfill:
                    try:
                        frame, extra = await _with_earlier_page(
                            kis, frame, symbol=symbol, venue=venue
                        )
                        backfill_pages += extra
                        reads += extra
                    except Exception as exc:  # noqa: BLE001 - keep the first page
                        errors.append(
                            {
                                "symbol": symbol,
                                "venue": venue,
                                "origin": "intraday_movers",
                                "error_class": type(exc).__name__,
                            }
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
        "backfill_symbols": len(backfill),
        "backfill_pages": backfill_pages,
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
    "CUT_REASON_BUDGET",
    "KisMinuteReader",
    "MinuteBar",
    "MinuteWriter",
    "SqlMinuteWriter",
    "TIER_ORDER",
    "TargetSet",
    "TierReport",
    "TierSpec",
    "TieredSelection",
    "select_tiered",
    "select_with_cap",
    "targets_report",
    "snapshot_top_symbols",
    "bars_from_frame",
    "build_targets",
    "collect",
    "is_closed",
    "load_nxt_eligible",
    "load_symbols_with_rows_today",
    "new_batch_id",
    "normalize_symbol",
    "parse_symbol_list",
    "resolve_us_exchanges",
]
