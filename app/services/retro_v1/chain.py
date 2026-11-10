"""The retrospective chain: candidate → proposal → rung → order → fill → markout → lot.

Loaders issue SELECTs only (each in its own SAVEPOINT via ``guarded``); the row
builders are pure functions of the loaded rows, so they are testable with
in-memory objects. Canonical rules are reused, not re-implemented:

* fills: ``execution_ledger_in_effect()`` (#1175) in SQL plus a defensive Python
  re-check, then ``supersede_provisional`` (ROB-1379) for websocket duplicates;
* lots: ``kis_lots.fifo_position`` (latest-seed FIFO over authoritative rows).

``linked_ref`` always points UPSTREAM to the event the row descends from. The
candidate→proposal link is not stored anywhere, so it is an inference (same
symbol, latest candidate recorded at or before the proposal) and the rows carry
``basis="inference"``.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import TradeForecast
from app.models.screener_pick_log import ScreenerPickLog
from app.services.execution_ledger.kis_lots import LedgerFill, fifo_position
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    normalize_order_id,
    supersede_provisional,
)
from app.services.retro_v1 import daily_bars
from app.services.retro_v1.envelope import (
    MAX_SOURCE_ROWS,
    Row,
    Unknowns,
    dec,
    guarded,
    iso,
    make_ref,
    safe_code,
)
from app.services.retro_v1.market import MarketInfo
from app.services.trade_journal.negative_class import NEGATIVE_CLASS_BUCKET

LIVE_PROPOSAL_ACCOUNT_MODES: Final = ("kis_live", "toss_live", "upbit")
MARKOUT_SESSIONS: Final = 20
#: Lot history needs a position's whole FIFO past, so its cap is higher.
MAX_LEDGER_ROWS: Final = 20000
#: Authority window around a day: a reconciler row stamped at order time must
#: still cover a websocket fill stamped later (same reason as kis_lots).
_AUTHORITY_PAD: Final = timedelta(days=2)
_BAR_LOOKAHEAD: Final = timedelta(days=60)
STAGE_ORDER: Final = (
    "candidate",
    "proposal",
    "rung",
    "order",
    "fill",
    "markout",
    "lot",
)


@dataclass(slots=True)
class ChainData:
    candidates: list[Any] = field(default_factory=list)
    forecasts: list[Any] = field(default_factory=list)
    proposals: list[Any] = field(default_factory=list)
    rungs: list[Any] = field(default_factory=list)
    fills: list[Any] = field(default_factory=list)
    history: list[Any] = field(default_factory=list)
    bars: list[daily_bars.Bar] = field(default_factory=list)
    superseded: int = 0
    #: sources whose read failed or hit its cap: the population is incomplete.
    incomplete: set[str] = field(default_factory=set)


def _symbol_filter(column: Any, symbols: list[str] | None) -> list[Any]:
    return [func.upper(column).in_(symbols)] if symbols else []


def _in_effect(rows: list[Any]) -> list[Any]:
    # SQL already filters; this keeps a leaked quarantined row out of every
    # fill/lot even if a caller's query forgot the predicate.
    return [r for r in rows if getattr(r, "quarantined_at", None) is None]


async def load_chain(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    *,
    symbols: list[str] | None,
    lo: datetime,
    hi: datetime,
    include_lots: bool = True,
) -> ChainData:
    """Read every chain source for the window. ``symbols=None`` = whole market.

    ``include_lots=False`` skips the downstream reads (lot history and markout
    bars) for callers that only count stages.
    """
    data = ChainData()

    async def read_candidates() -> list[Any]:
        stmt = (
            select(ScreenerPickLog)
            .where(ScreenerPickLog.market == market.key)
            .where(ScreenerPickLog.recorded_at >= lo, ScreenerPickLog.recorded_at < hi)
            .where(*_symbol_filter(ScreenerPickLog.symbol, symbols))
            .order_by(ScreenerPickLog.recorded_at, ScreenerPickLog.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def read_forecasts() -> list[Any]:
        stmt = (
            select(TradeForecast)
            .where(TradeForecast.decision_bucket == NEGATIVE_CLASS_BUCKET)
            .where(TradeForecast.instrument_type == market.instrument_type)
            .where(TradeForecast.created_at >= lo, TradeForecast.created_at < hi)
            .where(*_symbol_filter(TradeForecast.symbol, symbols))
            .order_by(TradeForecast.created_at, TradeForecast.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def read_proposals() -> list[Any]:
        stmt = (
            select(OrderProposal)
            .where(OrderProposal.market == market.instrument_type)
            .where(OrderProposal.account_mode.in_(LIVE_PROPOSAL_ACCOUNT_MODES))
            .where(OrderProposal.created_at >= lo, OrderProposal.created_at < hi)
            .where(*_symbol_filter(OrderProposal.symbol, symbols))
            .order_by(OrderProposal.created_at, OrderProposal.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    def fills_stmt(start: datetime | None, end: datetime, cap: int) -> Any:
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.instrument_type == market.instrument_type)
            .where(ExecutionLedger.account_mode == "live")
            .where(execution_ledger_in_effect())
            .where(ExecutionLedger.filled_at < end)
            .where(*_symbol_filter(ExecutionLedger.symbol, symbols))
        )
        if start is not None:
            stmt = stmt.where(ExecutionLedger.filled_at >= start)
        return stmt.order_by(ExecutionLedger.filled_at, ExecutionLedger.id).limit(cap)

    async def read_fills() -> list[Any]:
        stmt = fills_stmt(lo - _AUTHORITY_PAD, hi + _AUTHORITY_PAD, MAX_SOURCE_ROWS)
        return list((await session.execute(stmt)).scalars().all())

    async def read_history(fill_symbols: list[str]) -> list[Any]:
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.instrument_type == market.instrument_type)
            .where(ExecutionLedger.account_mode == "live")
            .where(execution_ledger_in_effect())
            .where(ExecutionLedger.filled_at < hi)
            .where(func.upper(ExecutionLedger.symbol).in_(fill_symbols))
            .order_by(ExecutionLedger.filled_at, ExecutionLedger.id)
            .limit(MAX_LEDGER_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    def track(name: str, rows: list[Any], ok: bool, cap: int) -> None:
        """A failed read (unknown already recorded) or a capped one is incomplete."""
        if not ok:
            data.incomplete.add(name)
        elif len(rows) >= cap:
            data.incomplete.add(name)
            unknowns.note(f"source.{name}", "incomplete_evidence")

    candidates, ok = await guarded(
        session, unknowns, "source.candidates", read_candidates, []
    )
    data.candidates = candidates
    track("candidates", candidates, ok, MAX_SOURCE_ROWS)

    forecasts, ok = await guarded(
        session, unknowns, "source.forecasts", read_forecasts, []
    )
    data.forecasts = forecasts
    track("forecasts", forecasts, ok, MAX_SOURCE_ROWS)

    proposals, ok = await guarded(
        session, unknowns, "source.proposals", read_proposals, []
    )
    data.proposals = proposals
    track("proposals", proposals, ok, MAX_SOURCE_ROWS)
    if ok:
        if proposals:

            async def read_rungs() -> list[Any]:
                stmt = (
                    select(OrderProposalRung)
                    .where(OrderProposalRung.proposal_pk.in_([p.id for p in proposals]))
                    .order_by(
                        OrderProposalRung.proposal_pk, OrderProposalRung.rung_index
                    )
                    .limit(MAX_SOURCE_ROWS * 4)
                )
                return list((await session.execute(stmt)).scalars().all())

            rungs, rung_ok = await guarded(
                session, unknowns, "source.rungs", read_rungs, []
            )
            data.rungs = rungs
            track("rungs", rungs, rung_ok, MAX_SOURCE_ROWS * 4)

    fills, ok = await guarded(session, unknowns, "source.fills", read_fills, [])
    track("fills", fills, ok, MAX_SOURCE_ROWS)
    if ok:
        live = _in_effect(fills)
        kept, dropped = supersede_provisional(live)
        data.superseded = dropped
        data.fills = [r for r in kept if lo <= r.filled_at < hi]

    if include_lots and data.fills:
        fill_symbols = sorted({r.symbol.upper() for r in data.fills})

        async def history() -> list[Any]:
            return await read_history(fill_symbols)

        rows, ok = await guarded(session, unknowns, "source.lots", history, [])
        track("lots", rows, ok, MAX_LEDGER_ROWS)
        if ok:
            data.history, _ = supersede_provisional(_in_effect(rows))

    if include_lots and data.fills:
        bar_symbols = sorted({r.symbol.upper() for r in data.fills})
        first = min(r.filled_at for r in data.fills)
        last = max(r.filled_at for r in data.fills)

        async def read_bars() -> list[daily_bars.Bar]:
            return await daily_bars.load_bars(
                session,
                market,
                "1d",
                bar_symbols,
                lo=first,
                hi=last + _BAR_LOOKAHEAD,
                cap=MAX_SOURCE_ROWS * 4,
            )

        bars, ok = await guarded(session, unknowns, "source.bars", read_bars, [])
        data.bars = bars
        track("bars", bars, ok, MAX_SOURCE_ROWS * 4)
    return data


# --------------------------------------------------------------------------
# pure row builders
# --------------------------------------------------------------------------
def _row(
    *,
    symbol: str,
    stage: str,
    event_at: datetime | None,
    event_ref: str,
    linked_ref: str | None,
    status: str,
    reason: str | None,
    quantity: Decimal | None,
    price: Decimal | str | None,
    market: MarketInfo,
    currency: str | None = None,
    basis: str = "inference",
    hints: dict[str, str] | None = None,
) -> Row:
    values = {
        "symbol": symbol,
        "stage": stage,
        "event_at": iso(event_at),
        "event_ref": event_ref,
        "linked_ref": linked_ref,
        "status": status,
        "reason": reason,
        "quantity": dec(quantity),
        "price": dec(price),
        "currency": currency or market.currency,
        "unit": market.quantity_unit,
        "basis": basis,
    }
    return values, dict(hints or {})


def candidate_rows(
    data: ChainData, market: MarketInfo
) -> list[tuple[datetime | None, Row]]:
    out: list[tuple[datetime | None, Row]] = []
    for c in data.candidates:
        out.append(
            (
                c.recorded_at,
                _row(
                    symbol=c.symbol,
                    stage="candidate",
                    event_at=c.recorded_at,
                    event_ref=make_ref("candidate", c.id),
                    linked_ref=None,
                    status=safe_code(c.admission, fallback="observed") or "observed",
                    reason=safe_code(c.admission_reason),
                    quantity=None,
                    price=c.decision_price_text,
                    market=market,
                    hints={
                        "linked_ref": "not_applicable",
                        "quantity": "not_applicable",
                        "price": "missing_data",
                    },
                ),
            )
        )
    for f in data.forecasts:
        out.append(
            (
                f.created_at,
                _row(
                    symbol=f.symbol,
                    stage="candidate",
                    event_at=f.created_at,
                    event_ref=make_ref("forecast", f.id),
                    linked_ref=None,
                    status=safe_code(f.decision_bucket) or NEGATIVE_CLASS_BUCKET,
                    reason="negative_class_forecast",
                    quantity=None,
                    price=None,
                    market=market,
                ),
            )
        )
    return out


def _match_candidate(proposal: Any, candidates: list[Any]) -> Any | None:
    earlier = [
        c
        for c in candidates
        if c.symbol.upper() == proposal.symbol.upper()
        and c.recorded_at <= proposal.created_at
    ]
    return max(earlier, key=lambda c: (c.recorded_at, c.id), default=None)


def proposal_rung_order_rows(
    data: ChainData, market: MarketInfo
) -> tuple[list[tuple[datetime | None, Row]], dict[tuple[str, str], str]]:
    """Proposal, rung and order rows, plus ``(symbol, order id) -> order ref``."""
    out: list[tuple[datetime | None, Row]] = []
    order_refs: dict[tuple[str, str], str] = {}
    rungs_by_proposal: dict[int, list[Any]] = defaultdict(list)
    for rung in data.rungs:
        rungs_by_proposal[rung.proposal_pk].append(rung)
    for p in data.proposals:
        rungs = rungs_by_proposal.get(p.id, [])
        candidate = _match_candidate(p, data.candidates)
        quantity = (
            sum((Decimal(str(r.quantity)) for r in rungs), Decimal(0))
            if rungs
            else None
        )
        proposal_ref = make_ref("proposal", p.id)
        out.append(
            (
                p.created_at,
                _row(
                    symbol=p.symbol,
                    stage="proposal",
                    event_at=p.created_at,
                    event_ref=proposal_ref,
                    linked_ref=make_ref("candidate", candidate.id)
                    if candidate
                    else None,
                    status=safe_code(p.lifecycle_state) or "unclassified",
                    reason=safe_code(p.void_reason),
                    quantity=quantity,
                    price=None,
                    market=market,
                    hints={"price": "not_applicable"},
                ),
            )
        )
        for r in rungs:
            rung_ref = make_ref("rung", r.id)
            limit_price = r.limit_price
            price_hint = {"price": "not_applicable"} if limit_price is None else {}
            reason = safe_code(r.void_reason_group) or safe_code(r.void_reason)
            out.append(
                (
                    r.created_at,
                    _row(
                        symbol=p.symbol,
                        stage="rung",
                        event_at=r.created_at,
                        event_ref=rung_ref,
                        linked_ref=proposal_ref,
                        status=safe_code(r.state) or "unclassified",
                        reason=reason,
                        quantity=Decimal(str(r.quantity)),
                        price=limit_price,
                        market=market,
                        hints=price_hint,
                    ),
                )
            )
            if r.broker_order_id:
                order_ref = make_ref("order", r.id)
                # The broker order number is only a join key here; it never
                # reaches a row or a ref.
                order_refs[
                    (p.symbol.upper(), normalize_order_id(r.broker_order_id))
                ] = order_ref
                out.append(
                    (
                        r.validated_at,
                        _row(
                            symbol=p.symbol,
                            stage="order",
                            event_at=r.validated_at,
                            event_ref=order_ref,
                            linked_ref=rung_ref,
                            status=safe_code(r.state) or "unclassified",
                            reason=None,
                            quantity=Decimal(str(r.quantity)),
                            price=limit_price,
                            market=market,
                            hints=price_hint,
                        ),
                    )
                )
    return out, order_refs


def fill_rows(
    data: ChainData,
    market: MarketInfo,
    order_refs: dict[tuple[str, str], str],
) -> list[tuple[datetime | None, Row]]:
    out: list[tuple[datetime | None, Row]] = []
    for f in data.fills:
        order_ref = order_refs.get(
            (f.symbol.upper(), normalize_order_id(f.broker_order_id))
        )
        out.append(
            (
                f.filled_at,
                _row(
                    symbol=f.symbol,
                    stage="fill",
                    event_at=f.filled_at,
                    event_ref=make_ref("fill", f.id),
                    linked_ref=order_ref,
                    status=f"{f.side}_filled",
                    reason=None if order_ref else "no_linked_order",
                    quantity=f.filled_qty,
                    price=f.filled_price,
                    market=market,
                    currency=f.currency,
                    # Only the broker's own order row (reconciler) is broker
                    # evidence; a websocket tap or manual import is not.
                    basis="broker" if f.source == "reconciler" else "inference",
                ),
            )
        )
    return out


def markout_rows(
    data: ChainData, market: MarketInfo
) -> list[tuple[datetime | None, Row]]:
    """Close of the ``MARKOUT_SESSIONS``-th bar strictly after each fill.

    A fill without that bar still gets a row: the missing bar is the unknown.
    """
    bars: dict[str, list[daily_bars.Bar]] = defaultdict(list)
    for bar in data.bars:
        bars[bar.symbol.upper()].append(bar)
    for series in bars.values():
        series.sort(key=lambda b: b.time)
    out: list[tuple[datetime | None, Row]] = []
    for f in data.fills:
        later = [b for b in bars.get(f.symbol.upper(), []) if b.time > f.filled_at]
        horizon = (
            later[MARKOUT_SESSIONS - 1] if len(later) >= MARKOUT_SESSIONS else None
        )
        out.append(
            (
                horizon.time if horizon else f.filled_at,
                _row(
                    symbol=f.symbol,
                    stage="markout",
                    event_at=horizon.time if horizon else None,
                    event_ref=make_ref("markout", f.id),
                    linked_ref=make_ref("fill", f.id),
                    status=f"markout_{MARKOUT_SESSIONS}_sessions",
                    reason=None if horizon else "missing_data",
                    quantity=f.filled_qty,
                    price=horizon.close if horizon else None,
                    market=market,
                    currency=f.currency,
                ),
            )
        )
    return out


def _ledger_fill(row: Any) -> LedgerFill:
    return LedgerFill(
        id=row.id,
        source=row.source,
        side=row.side,
        quantity=row.filled_qty,
        price=row.filled_price,
        filled_at=row.filled_at,
        broker_order_id=row.broker_order_id,
        venue=row.venue,
    )


def lot_rows(data: ChainData, market: MarketInfo) -> list[tuple[datetime | None, Row]]:
    """One lot row per (broker, symbol) that filled in the window.

    Net quantity and average cost of the remaining FIFO lots after the window,
    from authoritative rows only. A websocket row no authoritative row covers
    is provisional, and an oversold history is a gap: both make the lot unknown
    (null quantity and price), never a number that looks measured.
    """
    in_window: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for f in data.fills:
        in_window[(f.broker, f.symbol.upper())].append(f)
    history: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in data.history:
        history[(row.broker, row.symbol.upper())].append(row)
    out: list[tuple[datetime | None, Row]] = []
    for key, fills in sorted(in_window.items()):
        rows = history.get(key, [])
        authoritative = [r for r in rows if r.source in AUTHORITATIVE_SOURCES]
        provisional = [r for r in rows if r.source not in AUTHORITATIVE_SOURCES]
        last_fill = max(fills, key=lambda r: (r.filled_at, r.id))
        net, average, oversold = fifo_position([_ledger_fill(r) for r in authoritative])
        known = (
            bool(authoritative)
            and not provisional
            and oversold <= 0
            and "lots" not in data.incomplete
        )
        if known:
            status = "open" if net > 0 else "flat"
            quantity, price, reason = net, average, None
        else:
            status, quantity, price = "unknown", None, None
            reason = "incomplete_evidence"
        out.append(
            (
                last_fill.filled_at,
                _row(
                    symbol=last_fill.symbol,
                    stage="lot",
                    event_at=last_fill.filled_at,
                    event_ref=make_ref("lot", f"{key[0]}|{key[1]}|{last_fill.id}"),
                    linked_ref=make_ref("fill", last_fill.id),
                    status=status,
                    reason=reason,
                    quantity=quantity,
                    price=price,
                    market=market,
                    currency=last_fill.currency,
                    hints={"price": "not_applicable"} if status == "flat" else {},
                ),
            )
        )
    return out


def build_chain(
    data: ChainData, market: MarketInfo, *, include_lots: bool
) -> list[Row]:
    """All chain rows of the cohort, in a stable (time, stage, ref) order."""
    stage_rank = {name: i for i, name in enumerate(STAGE_ORDER)}
    rows = candidate_rows(data, market)
    chain_rows, order_refs = proposal_rung_order_rows(data, market)
    rows += chain_rows
    rows += fill_rows(data, market, order_refs)
    rows += markout_rows(data, market)
    if include_lots:
        rows += lot_rows(data, market)
    rows.sort(
        key=lambda item: (
            item[0] is None,
            item[0].timestamp() if item[0] is not None else 0.0,
            stage_rank[item[1][0]["stage"]],
            item[1][0]["event_ref"],
        )
    )
    return [row for _, row in rows]


def data_as_of(data: ChainData) -> datetime | None:
    times: list[datetime] = []
    times += [c.recorded_at for c in data.candidates]
    times += [f.created_at for f in data.forecasts]
    times += [p.created_at for p in data.proposals]
    times += [f.filled_at for f in data.fills]
    aware = [t for t in times if t is not None and t.tzinfo is not None]
    return max(aware) if aware else None
