"""``retro_timeline`` — one market's per-symbol evidence timeline for one KST date.

hk 6171. For a trading date (and optionally one symbol) it answers, symbol by
symbol, *why nothing was bought*: which of the separately stored records exist
for the symbol and which closed ``state`` they add up to. Read-only: SELECT
inside the reader's own snapshot (``read_snapshot``), every source group in a
SAVEPOINT (``guarded``), so a failed or capped read is an ``unknowns`` entry and
a null state — never a weaker state.

Sources and their date basis (all are the KST calendar day of the request unless
stated; US rows have no ET trading-day column except the minute table):

* ``observation_log`` — ``invest_momentum_event_snapshots.trading_date`` (KST
  date it stores), first capture per symbol, KR only.
* ``pick_log`` — ``review.screener_pick_log.recorded_at`` in the KST window.
* ``decision`` — ``review.decision_evidence.decided_at`` and the
  ``deferred_no_action`` forecasts (``trade_forecasts.created_at``) in the window.
* ``proposal`` / ``order`` / ``fill`` — the existing chain readers
  (``chain.load_chain`` + the proposal/order/fill row builders), same window.
* minute bars — KR ``research.kr_candles_1m.time_utc`` in the KST window; US
  ``research.us_candles_1m.session_date`` equal to the requested date (the ET
  trading day that table stores). Only row counts and first/last bar times.

The observation log writer lives in ``app.services.invest_momentum_events``, whose
module imports a broker package; the retro/v1 import guard forbids that, so the
tables are read here directly (SELECT only).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Final

from sqlalchemy import bindparam, distinct, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invest_momentum_event_snapshot import (
    InvestMomentumEventRun,
    InvestMomentumEventSnapshot,
)
from app.models.screener_pick_log import ScreenerPickLog
from app.services.retro_v1.chain import (
    ChainData,
    _symbol_filter,
    fill_rows,
    load_chain,
    order_rows,
    proposal_rung_rows,
)
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    MAX_SOURCE_ROWS,
    Row,
    Unknowns,
    build_response,
    check_limit,
    check_symbol,
    decode_cursor,
    guarded,
    iso,
    kst_window,
    latest,
    make_ref,
    params_digest,
    parse_date,
    read_snapshot,
    safe_code,
)
from app.services.retro_v1.links import decision_ref
from app.services.retro_v1.market import (
    MarketInfo,
    normalize_symbol,
    resolve_market,
    select_market,
)
from app.services.retro_v1.retro_decisions import DECISIONS

#: Closed event sources, in the order that breaks ties between equal instants.
SOURCES: Final = (
    "observation_log",
    "pick_log",
    "decision",
    "proposal",
    "order",
    "fill",
)
STATES: Final = (
    "not_observed",
    "observed_only",
    "candidate_not_admitted",
    "admitted_no_record",
    "admitted_held",
    "proposed_unfilled",
    "proposed_filled",
)
#: Source groups whose read can fail or be cut off; a state that one of them
#: could have hidden is null, not weaker.
GROUPS: Final = ("observation_log", "pick_log", "decisions", "orders")
SOURCE_UNAVAILABLE: Final = "source_unavailable"
#: ``load_chain`` source names → the group whose evidence they carry. Link,
#: send-intent and candidate-reference reads only refine refs, never a state.
_CHAIN_GROUP: Final = {
    "candidates": "pick_log",
    "forecasts": "decisions",
    "proposals": "orders",
    "rungs": "orders",
    "kis_orders": "orders",
    "live_orders": "orders",
    "toss_orders": "orders",
    "fills": "orders",
}
_FILLED_ORDER_KINDS: Final = frozenset({"filled", "partially_filled"})
_NOT_ADMITTED_KINDS: Final = frozenset({"not_admitted", "dropped_preselection"})
_DECLINED_KINDS: Final = frozenset({"declined", "deferred_no_action"})
_PROPOSED_DECISION_KINDS: Final = frozenset({"proposal", "direct_order"})
_MINUTE_CAP: Final = MAX_SOURCE_ROWS * 4

_KR_MINUTES: Final = """
    SELECT symbol, venue, count(*) AS bar_count,
           min(time_utc) AS first_bar_at, max(time_utc) AS last_bar_at
    FROM research.kr_candles_1m
    WHERE symbol IN :symbols AND time_utc >= :lo AND time_utc < :hi
    GROUP BY symbol, venue
    ORDER BY symbol, venue
    LIMIT :cap
"""
_US_MINUTES: Final = """
    SELECT symbol, venue, count(*) AS bar_count,
           min(time_utc) AS first_bar_at, max(time_utc) AS last_bar_at
    FROM research.us_candles_1m
    WHERE symbol IN :symbols AND session_date = :day
    GROUP BY symbol, venue
    ORDER BY symbol, venue
    LIMIT :cap
"""


# --------------------------------------------------------------------------
# pure state derivation
# --------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Event:
    """One piece of evidence. ``kind``/``reason`` are closed codes of the source."""

    at: datetime
    source: str
    kind: str
    reason: str | None = None
    ref: str | None = None

    def sort_key(self) -> tuple[float, int, str, str]:
        return (
            self.at.timestamp(),
            SOURCES.index(self.source),
            self.ref or "",
            self.kind,
        )


@dataclass(frozen=True, slots=True)
class Timeline:
    state: str | None
    reason: str
    events: tuple[Event, ...]


def _latest(events: Iterable[Event]) -> Event | None:
    return max(events, key=Event.sort_key, default=None)


def derive_state(
    events: Sequence[Event], unavailable: Collection[str] = frozenset()
) -> tuple[str | None, str]:
    """``(state, reason)`` of one symbol's events.

    Precedence: proposed_* > admitted_held > admitted_no_record >
    candidate_not_admitted > observed_only > not_observed. ``unavailable`` names
    the source groups (``GROUPS``) that failed or were cut off: when one of them
    could have produced a higher state than the one the visible events give, the
    answer is ``(None, "source_unavailable")`` — never the weaker state.
    """
    gone = frozenset(unavailable)
    picks = [e for e in events if e.source == "pick_log"]
    decisions = [e for e in events if e.source == "decision"]
    proposals = [e for e in events if e.source == "proposal"]
    orders = [e for e in events if e.source == "order"]
    fills = [e for e in events if e.source == "fill"]

    proposed = bool(
        proposals
        or orders
        or fills
        or any(d.kind in _PROPOSED_DECISION_KINDS for d in decisions)
    )
    if proposed:
        if fills or any(o.kind in _FILLED_ORDER_KINDS for o in orders):
            if any(o.kind == "filled" for o in orders):
                return "proposed_filled", "filled"
            if any(o.kind == "partially_filled" for o in orders):
                return "proposed_filled", "partially_filled"
            return "proposed_filled", "fill_event_only"
        if "orders" in gone:
            return None, SOURCE_UNAVAILABLE
        last_order = _latest(orders)
        if last_order is not None:
            return "proposed_unfilled", f"order_{last_order.kind}"
        last_proposal = _latest(proposals)
        if last_proposal is not None:
            return "proposed_unfilled", (
                last_proposal.reason or f"proposal_{last_proposal.kind}"
            )
        return "proposed_unfilled", "decision_without_order"
    # A proposal, an order or a recorded decision can hide behind either group.
    if gone & {"orders", "decisions"}:
        return None, SOURCE_UNAVAILABLE

    admitted = [p for p in picks if p.kind == "admitted"]
    not_admitted = [p for p in picks if p.kind in _NOT_ADMITTED_KINDS]
    declined = [d for d in decisions if d.kind in _DECLINED_KINDS]
    if admitted:
        if declined:
            return "admitted_held", (
                "declined_decision"
                if any(d.kind == "declined" for d in declined)
                else "deferred_no_action_forecast"
            )
        return "admitted_no_record", "no_decision_record"
    if "pick_log" in gone:
        return None, SOURCE_UNAVAILABLE
    if not_admitted:
        last = _latest(not_admitted)
        assert last is not None
        return "candidate_not_admitted", last.reason or last.kind
    if declined:
        return "admitted_held", "declined_without_pick_record"
    if any(e.source == "observation_log" for e in events):
        return "observed_only", "observation_log_only"
    if picks:
        return "observed_only", "pick_admission_unrecorded"
    if "observation_log" in gone:
        return None, SOURCE_UNAVAILABLE
    return "not_observed", "no_record_in_sources"


def assemble(
    events: Sequence[Event], unavailable: Collection[str] = frozenset()
) -> Timeline:
    """State, reason and the events in time order (ties: source, ref)."""
    state, reason = derive_state(events, unavailable)
    return Timeline(state, reason, tuple(sorted(events, key=Event.sort_key)))


# --------------------------------------------------------------------------
# source reads
# --------------------------------------------------------------------------
def _key(symbol: str, market: MarketInfo) -> str:
    return normalize_symbol(symbol, market)


def _chain_events(
    data: ChainData, market: MarketInfo, unknowns: Unknowns
) -> tuple[list[tuple[str, Event]], bool]:
    """Proposal, order and fill rows of the chain as ``(symbol, event)``.

    The second value is ``False`` when a row had no instant (never expected from
    the ledgers): the evidence is then treated as incomplete, not dropped.
    """
    complete = True
    out: list[tuple[str, Event]] = []

    def add(source: str, rows: list[tuple[datetime | None, Row]]) -> None:
        nonlocal complete
        for at, (row, _hints) in rows:
            if row["stage"] != source:
                continue
            if at is None or at.tzinfo is None:
                complete = False
                unknowns.note(f"source.{source}_events", "incomplete_evidence")
                continue
            reason = row.get("block_reason") or row.get("reason")
            out.append(
                (
                    _key(str(row["symbol"]), market),
                    Event(
                        at=at,
                        source=source,
                        kind=safe_code(row["status"]) or "unclassified",
                        reason=safe_code(reason, fallback=None),
                        ref=row["event_ref"],
                    ),
                )
            )

    add("proposal", proposal_rung_rows(data, market))
    orders, refs = order_rows(data, market)
    add("order", orders)
    add("fill", fill_rows(data, market, refs))
    return out, complete


async def _observation_events(
    session: AsyncSession,
    unknowns: Unknowns,
    day: date,
    symbols: list[str] | None,
) -> tuple[list[tuple[str, Event]], bool]:
    row = InvestMomentumEventSnapshot

    async def read() -> list[Any]:
        stmt = (
            select(row)
            .where(row.market == "kr", row.trading_date == day)
            .where(*_symbol_filter(row.symbol, symbols))
            .distinct(row.symbol)
            .order_by(
                row.symbol.asc(),
                row.snapshot_at.asc(),
                row.rank.asc(),
                row.order_type.asc(),
                row.trade_type.asc().nulls_last(),
                row.market_type.asc().nulls_last(),
            )
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    rows, ok = await guarded(session, unknowns, "source.observation_log", read, [])
    if ok and len(rows) >= MAX_SOURCE_ROWS:
        unknowns.note("source.observation_log", "incomplete_evidence")
        ok = False
    events = [
        (
            str(r.symbol).strip().upper(),
            Event(
                at=r.snapshot_at,
                source="observation_log",
                kind=safe_code(r.order_type) or "unclassified",
            ),
        )
        for r in rows
    ]
    return events, ok


async def _observation_runs(
    session: AsyncSession, unknowns: Unknowns, day: date
) -> dict[str, int] | None:
    run = InvestMomentumEventRun

    async def read() -> dict[str, int]:
        stmt = (
            select(run.status, func.count())
            .where(run.trading_date == day)
            .group_by(run.status)
        )
        return {str(s): int(n) for s, n in (await session.execute(stmt)).all()}

    counts, ok = await guarded(session, unknowns, "coverage.observation_log", read, {})
    return counts if ok else None


async def _pick_calls(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    lo: datetime,
    hi: datetime,
) -> int | None:
    async def read() -> int:
        stmt = (
            select(func.count(distinct(ScreenerPickLog.call_id)))
            .where(ScreenerPickLog.market == market.key)
            .where(ScreenerPickLog.recorded_at >= lo, ScreenerPickLog.recorded_at < hi)
        )
        return int((await session.execute(stmt)).scalar_one())

    calls, ok = await guarded(session, unknowns, "coverage.pick_log", read, 0)
    return calls if ok else None


async def _decision_events(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    lo: datetime,
    hi: datetime,
    symbols: list[str] | None,
) -> tuple[list[tuple[str, Event]], bool]:
    cols = DECISIONS.c

    async def read() -> list[Any]:
        stmt = (
            select(cols.decision_id, cols.decision_kind, cols.decided_at, cols.symbol)
            .where(cols.market == market.instrument_type)
            .where(cols.decided_at >= lo, cols.decided_at < hi)
            .where(*_symbol_filter(cols.symbol, symbols))
            .order_by(cols.decided_at, cols.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).all())

    rows, ok = await guarded(session, unknowns, "source.decisions", read, [])
    if ok and len(rows) >= MAX_SOURCE_ROWS:
        unknowns.note("source.decisions", "incomplete_evidence")
        ok = False
    events = [
        (
            _key(str(r.symbol), market),
            Event(
                at=r.decided_at,
                source="decision",
                kind=safe_code(r.decision_kind) or "unclassified",
                ref=decision_ref(r.decision_id),
            ),
        )
        for r in rows
    ]
    return events, ok


async def _minute_bars(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    day: date,
    lo: datetime,
    hi: datetime,
    symbols: list[str],
) -> dict[str, list[dict[str, Any]]] | None:
    """Per symbol, per venue: bar count and first/last bar start. ``None`` = unread."""
    if not symbols:
        return {}

    async def read() -> list[Any]:
        sql = _KR_MINUTES if market.key == "kr" else _US_MINUTES
        stmt = text(sql).bindparams(bindparam("symbols", expanding=True))
        params: dict[str, Any] = {"symbols": symbols, "cap": _MINUTE_CAP}
        if market.key == "kr":
            params.update(lo=lo, hi=hi)
        else:
            params["day"] = day
        return list((await session.execute(stmt, params)).mappings().all())

    rows, ok = await guarded(session, unknowns, "source.minute_bars", read, [])
    if not ok:
        return None
    if len(rows) >= _MINUTE_CAP:
        unknowns.note("source.minute_bars", "incomplete_evidence")
        return None
    out: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        out[str(r["symbol"]).upper()].append(
            {
                "venue": safe_code(r["venue"]) or "unclassified",
                "rows": int(r["bar_count"]),
                "first_bar_at": iso(r["first_bar_at"]),
                "last_bar_at": iso(r["last_bar_at"]),
            }
        )
    return dict(out)


def _forecast_events(
    data: ChainData, market: MarketInfo
) -> list[tuple[str, Event]]:
    return [
        (
            _key(str(f.symbol), market),
            Event(
                at=f.created_at,
                source="decision",
                kind=safe_code(f.decision_bucket) or "unclassified",
                ref=make_ref("forecast", f.id),
            ),
        )
        for f in data.forecasts
    ]


def _pick_events(data: ChainData, market: MarketInfo) -> list[tuple[str, Event]]:
    return [
        (
            _key(str(c.symbol), market),
            Event(
                at=c.recorded_at,
                source="pick_log",
                kind=safe_code(c.admission, fallback="unrecorded") or "unrecorded",
                reason=safe_code(c.admission_reason, fallback=None),
                ref=make_ref("candidate", c.id),
            ),
        )
        for c in data.candidates
    ]


# --------------------------------------------------------------------------
# reader
# --------------------------------------------------------------------------
def _event_body(event: Event) -> dict[str, Any]:
    return {
        "at": iso(event.at),
        "source": event.source,
        "kind": event.kind,
        "reason": event.reason,
        "ref": event.ref,
    }


def _coverage(
    market: MarketInfo,
    runs: dict[str, int] | None,
    pick_calls: int | None,
    minute_status: str,
) -> dict[str, Any]:
    if market.key != "kr":
        observation: dict[str, Any] = {
            "status": "unsupported_market",
            "runs": None,
        }
    elif runs is None:
        observation = {"status": SOURCE_UNAVAILABLE, "runs": None}
    else:
        counts = {name: runs.get(name, 0) for name in ("ok", "empty", "error")}
        observation = {
            "status": "runs_recorded"
            if sum(counts.values())
            else "observation_log_not_running",
            "runs": counts,
        }
    if pick_calls is None:
        picks: dict[str, Any] = {"status": SOURCE_UNAVAILABLE, "calls": None}
    else:
        picks = {
            "status": "calls_recorded" if pick_calls else "no_calls",
            "calls": pick_calls,
        }
    return {
        "observation_log": observation,
        "pick_log": picks,
        "minute_bars": {"status": minute_status},
    }


async def read_retro_timeline(
    session: AsyncSession,
    *,
    date: Any,
    market: str,
    symbol: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Contract ``retro_timeline``: one row per symbol seen in any source."""
    check_limit(limit)
    day = parse_date(date)
    requested = check_symbol(symbol) if symbol is not None else None
    # ``select_market`` validates the symbol's shape against the market.
    info = select_market(requested, market) if requested else resolve_market(market)
    wanted = normalize_symbol(requested, info) if requested else None
    symbols = [wanted] if wanted else None
    digest = params_digest(
        op="retro_timeline", market=info.key, day=day, symbol=wanted or ""
    )
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(day, day)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)

    unavailable: set[str] = set()
    events: dict[str, list[Event]] = defaultdict(list)

    def take(found: list[tuple[str, Event]]) -> None:
        for key, event in found:
            events[key].append(event)

    async with read_snapshot(session):
        data = await load_chain(
            session,
            unknowns,
            info,
            symbols=symbols,
            lo=lo,
            hi=hi,
            include_lots=False,
            include_links=True,
        )
        unavailable |= {
            _CHAIN_GROUP[name] for name in data.incomplete if name in _CHAIN_GROUP
        }
        take(_pick_events(data, info))
        take(_forecast_events(data, info))
        chain_events, chain_complete = _chain_events(data, info, unknowns)
        take(chain_events)
        if not chain_complete:
            unavailable.add("orders")

        decision_events, decisions_ok = await _decision_events(
            session, unknowns, info, lo, hi, symbols
        )
        take(decision_events)
        if not decisions_ok:
            unavailable.add("decisions")

        runs: dict[str, int] | None = None
        if info.key == "kr":
            observed, observed_ok = await _observation_events(
                session, unknowns, day, symbols
            )
            take(observed)
            if not observed_ok:
                unavailable.add("observation_log")
            runs = await _observation_runs(session, unknowns, day)
        pick_calls = await _pick_calls(session, unknowns, info, lo, hi)

        population = sorted(set(events) | ({wanted} if wanted else set()))
        minutes: dict[str, list[dict[str, Any]]] | None = None
        if info.key in {"kr", "us"}:
            minutes = await _minute_bars(
                session, unknowns, info, day, lo, hi, population
            )
            minute_status = "available" if minutes is not None else SOURCE_UNAVAILABLE
        else:
            minute_status = "unsupported_market"

    rows: list[Row] = []
    for key in population:
        timeline = assemble(events.get(key, []), unavailable)
        if minutes is not None:
            bars: list[dict[str, Any]] | None = minutes.get(key, [])
            bars_reason = None
        else:
            bars = None
            bars_reason = (
                "unsupported_market"
                if minute_status == "unsupported_market"
                else SOURCE_UNAVAILABLE
            )
        row: dict[str, Any] = {
            "symbol": key,
            "market": info.key,
            "state": timeline.state,
            "reason": timeline.reason,
            "events": [_event_body(e) for e in timeline.events],
            "minute_bars": bars,
        }
        if bars_reason is not None:
            row["minute_bars_reason"] = bars_reason
        rows.append((row, {}))

    all_events = [e for found in events.values() for e in found]
    return {
        **build_response(
            as_of=as_of,
            data_as_of=latest(e.at for e in all_events),
            rows=rows,
            total=None if unavailable else len(rows),
            limit=limit,
            cursor=cursor,
            digest=digest,
            offset=offset,
            unknowns=unknowns,
        ),
        "coverage": _coverage(info, runs, pick_calls, minute_status),
    }
