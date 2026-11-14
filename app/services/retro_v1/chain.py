"""The retrospective chain: candidate → proposal → rung → order → fill → markout → lot.

Loaders issue SELECTs only (each in its own SAVEPOINT via ``guarded``); the row
builders are pure functions of the loaded rows, so they are testable with
in-memory objects. Canonical rules are reused, not re-implemented:

* fills: ``execution_ledger_in_effect()`` (#1175) in SQL plus a defensive Python
  re-check, then ``supersede_provisional`` (ROB-1379) for websocket duplicates;
* lots: ``kis_lots.fifo_position`` (latest-seed FIFO over authoritative rows).

``linked_ref`` always points UPSTREAM to the event the row descends from. Rows
say how it was made in ``link_basis`` (``recorded`` = an exact stored key,
``inference`` = a heuristic) and, for proposal, rung and order rows, which
decision they descend from in ``decision_ref`` (hk 4188, design #4154 §3):

* candidate→proposal: the candidate reference stored on the proposal's decision
  (``recorded``/the writer's labelled ``inference``). A proposal without a
  decision keeps the old inference (same symbol, latest candidate recorded at
  or before the proposal), capped to the proposal's KST day (Q20);
* order→rung/decision: ``links.resolve_attempts`` (§3 row 4), then, with no live
  attempt, the legacy rung match (idempotency key ``recorded``, correlation
  only ``inference``).

Optional link fields are left out of a row when they have no value.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Final

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger, execution_ledger_in_effect
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    OrderSendIntent,
    TossLiveOrderLedger,
    TradeForecast,
)
from app.models.screener_pick_log import ScreenerPickLog
from app.services.execution_ledger.kis_lots import LedgerFill, fifo_position
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    OrderIdentity,
    order_identity,
    order_trading_day,
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
from app.services.retro_v1.links import (
    LINK_BASES,
    AttemptResolution,
    OrderLinkEvidence,
    decision_ref,
    kst_day,
    load_decisions,
    load_order_links,
    order_send_key,
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
    link_rungs: list[Any] = field(default_factory=list)
    link_proposals: list[Any] = field(default_factory=list)
    #: rung idempotency key -> when its KIS send was reserved (``None`` = the
    #: key matched several intent rows, so the day is not established).
    send_times: dict[str, datetime | None] = field(default_factory=dict)
    orders: list[tuple[str, Any]] = field(default_factory=list)
    link_orders: list[tuple[str, Any]] = field(default_factory=list)
    #: decision attempt/outcome rows of the window's orders (§3 row 4).
    order_links: OrderLinkEvidence = field(default_factory=OrderLinkEvidence.empty)
    #: decision rows (candidate reference columns) by ``decision_id``.
    decisions: dict[Any, Any] = field(default_factory=dict)
    #: pick rows named by a stored candidate reference (any date).
    ref_candidates: list[Any] = field(default_factory=list)
    evidence_fills: list[Any] = field(default_factory=list)
    fills: list[Any] = field(default_factory=list)
    history: list[Any] = field(default_factory=list)
    bars: list[daily_bars.Bar] = field(default_factory=list)
    superseded: int = 0
    #: sources whose read failed or hit its cap: the population is incomplete.
    incomplete: set[str] = field(default_factory=set)


def _symbol_filter(column: Any, symbols: list[str] | None) -> list[Any]:
    return [func.upper(column).in_(symbols)] if symbols else []


#: Keys are unique only within an ``account_scope`` (kis_mock and others share
#: the table); a live order's send evidence is its ``kis_live`` reservation only.
KIS_LIVE_SEND_SCOPE: Final = "kis_live"
_SEED_ORDER_ID_PREFIX: Final = "SEED-"
_BROKER_BY_ACCOUNT_MODE: Final = {
    "kis_live": "kis",
    "toss_live": "toss",
    "upbit": "upbit",
}


def fills_stmt(
    market: MarketInfo,
    symbols: list[str] | None,
    start: datetime | None,
    end: datetime,
    cap: int,
) -> Any:
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


async def read_history(
    session: AsyncSession,
    market: MarketInfo,
    hi: datetime,
    fill_symbols: list[str],
) -> list[Any]:
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


def is_opening_seed(row: Any) -> bool:
    """A ``manual_import`` opening-lot seed: a position snapshot, not an execution.

    Same rule as ``kis_lots._is_opening_seed`` (``SEED-*`` order id), plus the
    ``seed_kind=opening_lot`` marker the seed writer stores. Seeds stay in the
    FIFO history that sizes lots; they are never a fill event or a markout.
    """
    if row.source != "manual_import":
        return False
    raw = getattr(row, "raw_payload_json", None)
    return str(row.broker_order_id).startswith(_SEED_ORDER_ID_PREFIX) or (
        isinstance(raw, dict) and raw.get("seed_kind") == "opening_lot"
    )


def rung_identity(
    proposal: Any,
    rung: Any,
    market: MarketInfo,
    send_times: dict[str, datetime | None],
) -> OrderIdentity | None:
    """The canonical ROB-1379 order identity of the order a rung sent.

    The same tuple a ledger row gets from ``order_identity`` (broker, mode,
    instrument, symbol, side, currency, normalized order number, trading day),
    so a reused KIS order number on another day or broker cannot match.

    Only KIS identities carry a trading day, and the rung's own ``validated_at``
    is not evidence of it (cancel and fill transitions overwrite it). The day
    comes from ``review.order_send_intents.created_at``, inserted once right
    before the POST under the rung's idempotency key. ``None`` when that
    evidence is missing or ambiguous (or the broker is unknown): the link and
    the funnel's fill count are then unknown rather than guessed.
    """
    broker = _BROKER_BY_ACCOUNT_MODE.get(proposal.account_mode)
    if broker is None or not rung.broker_order_id:
        return None
    if broker == "kis":
        sent_at = send_times.get(rung.idempotency_key) if rung.idempotency_key else None
    else:
        sent_at = rung.created_at  # the identity of non-KIS brokers has no day
    if sent_at is None:
        return None
    return order_identity(
        SimpleNamespace(
            broker=broker,
            account_mode="live",
            instrument_type=market.instrument_type,
            symbol=proposal.symbol,
            side=rung.side,
            broker_order_id=rung.broker_order_id,
            currency=market.currency,
            filled_at=sent_at,
        )
    )


async def load_chain(
    session: AsyncSession,
    unknowns: Unknowns,
    market: MarketInfo,
    *,
    symbols: list[str] | None,
    lo: datetime,
    hi: datetime,
    include_lots: bool = True,
    include_links: bool = True,
) -> ChainData:
    """Read every chain source for the window. ``symbols=None`` = whole market.

    ``include_lots=False`` skips the downstream reads (lot history and markout
    bars) for callers that only count stages. ``include_links=False`` skips the
    decision link reads (§3) for callers that do not show links.
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

    # Reference ledger and filter so load_chain is counted as an in-effect reader on disk
    _ = (ExecutionLedger, execution_ledger_in_effect)

    async def read_fills() -> list[Any]:
        stmt = fills_stmt(
            market, symbols, lo - _AUTHORITY_PAD, hi + _AUTHORITY_PAD, MAX_SOURCE_ROWS
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
            kis_proposals = {
                p.id for p in proposals if p.account_mode == KIS_LIVE_SEND_SCOPE
            }
            keys = sorted(
                {
                    r.idempotency_key
                    for r in rungs
                    if r.idempotency_key
                    and r.broker_order_id
                    and r.proposal_pk in kis_proposals
                }
            )
            if rung_ok and keys:

                async def read_send_intents() -> list[Any]:
                    stmt = select(
                        OrderSendIntent.idempotency_key, OrderSendIntent.created_at
                    ).where(
                        OrderSendIntent.account_scope == KIS_LIVE_SEND_SCOPE,
                        OrderSendIntent.idempotency_key.in_(keys),
                    )
                    return list((await session.execute(stmt)).all())

                intents, intent_ok = await guarded(
                    session, unknowns, "source.send_intents", read_send_intents, []
                )
                track("send_intents", intents, intent_ok, MAX_SOURCE_ROWS * 4)
                for key, created in intents:
                    data.send_times[key] = None if key in data.send_times else created

    for name, model in (
        ("kis_orders", KISLiveOrderLedger),
        ("live_orders", LiveOrderLedger),
        ("toss_orders", TossLiveOrderLedger),
    ):

        async def read_orders(model: Any = model) -> list[Any]:
            stmt = select(model).where(
                model.trade_date >= lo - _AUTHORITY_PAD,
                model.trade_date < hi + _AUTHORITY_PAD,
            )
            stmt = stmt.where(*_symbol_filter(model.symbol, symbols))
            if model is KISLiveOrderLedger:
                stmt = stmt.where(
                    model.account_mode == "kis_live",
                    model.instrument_type == market.instrument_type,
                )
            elif model is LiveOrderLedger:
                stmt = stmt.where(
                    model.market == market.key,
                    model.account_scope.in_(("kis_live", "upbit_live")),
                )
            else:
                stmt = stmt.where(
                    model.market == market.key,
                    model.account_mode == "toss_live",
                    model.operation_kind == "place",
                )
            stmt = stmt.order_by(model.trade_date, model.id).limit(MAX_SOURCE_ROWS)
            return list((await session.execute(stmt)).scalars().all())

        orders, order_ok = await guarded(
            session, unknowns, f"source.{name}", read_orders, []
        )
        track(name, orders, order_ok, MAX_SOURCE_ROWS)
        data.orders.extend(
            (name, order) for order in orders if lo <= order.trade_date < hi
        )
        data.link_orders.extend(
            (name, order) for order in orders if not lo <= order.trade_date < hi
        )

    keys = {
        getattr(o, "idempotency_key", None) or getattr(o, "client_order_id", None)
        for _, o in data.orders
    } - {None, ""}
    correlations = {getattr(o, "correlation_id", None) for _, o in data.orders} - {
        None,
        "",
    }
    if keys or correlations:

        async def read_link_rungs() -> list[Any]:
            stmt = (
                select(OrderProposalRung)
                .where(
                    or_(
                        OrderProposalRung.idempotency_key.in_(keys),
                        OrderProposalRung.correlation_id.in_(correlations),
                    )
                )
                .order_by(OrderProposalRung.id)
                .limit(MAX_SOURCE_ROWS * 4)
            )
            return list((await session.execute(stmt)).scalars().all())

        found, link_ok = await guarded(
            session, unknowns, "source.link_rungs", read_link_rungs, []
        )
        track("link_rungs", found, link_ok, MAX_SOURCE_ROWS * 4)
        seen = {r.id for r in data.rungs}
        data.link_rungs = [r for r in found if r.id not in seen]
        proposal_ids = {r.proposal_pk for r in data.link_rungs} - {
            p.id for p in data.proposals
        }
        if proposal_ids:

            async def read_link_proposals() -> list[Any]:
                stmt = (
                    select(OrderProposal)
                    .where(
                        OrderProposal.id.in_(proposal_ids),
                        OrderProposal.market == market.instrument_type,
                        OrderProposal.account_mode.in_(LIVE_PROPOSAL_ACCOUNT_MODES),
                    )
                    .order_by(OrderProposal.id)
                    .limit(MAX_SOURCE_ROWS)
                )
                return list((await session.execute(stmt)).scalars().all())

            linked, linked_ok = await guarded(
                session, unknowns, "source.link_proposals", read_link_proposals, []
            )
            track("link_proposals", linked, linked_ok, MAX_SOURCE_ROWS)
            data.link_proposals = linked
        extra_keys = {
            r.idempotency_key for r in data.link_rungs if r.idempotency_key
        } - data.send_times.keys()
        if extra_keys:

            async def read_link_intents() -> list[Any]:
                stmt = select(
                    OrderSendIntent.idempotency_key, OrderSendIntent.created_at
                ).where(
                    OrderSendIntent.account_scope == KIS_LIVE_SEND_SCOPE,
                    OrderSendIntent.idempotency_key.in_(extra_keys),
                )
                return list((await session.execute(stmt)).all())

            intents, intent_ok = await guarded(
                session, unknowns, "source.link_intents", read_link_intents, []
            )
            track("link_intents", intents, intent_ok, MAX_SOURCE_ROWS * 4)
            for key, created in intents:
                data.send_times[key] = None if key in data.send_times else created

    if include_links:
        data.order_links = await load_order_links(
            session,
            unknowns,
            [k for k in (order_send_key(o) for _, o in data.orders) if k],
        )
        # An attempt can name an out-of-window proposal's rung; without
        # loading it the stored (proposal_id, rung_index) binding could not
        # produce a linked_ref.
        bound_proposal_ids = {
            attempt.proposal_id
            for attempts in data.order_links.attempts.values()
            for attempt in attempts
            if attempt.proposal_id is not None and attempt.rung_index is not None
        }
        known_pids = {
            p.proposal_id
            for p in [*data.proposals, *data.link_proposals]
            if getattr(p, "proposal_id", None) is not None
        }
        missing = sorted(bound_proposal_ids - known_pids, key=str)
        if missing:

            async def read_bound_proposals() -> list[Any]:
                stmt = (
                    select(OrderProposal)
                    .where(
                        OrderProposal.proposal_id.in_(missing),
                        OrderProposal.market == market.instrument_type,
                        OrderProposal.account_mode.in_(LIVE_PROPOSAL_ACCOUNT_MODES),
                    )
                    .order_by(OrderProposal.id)
                    .limit(MAX_SOURCE_ROWS)
                )
                return list((await session.execute(stmt)).scalars().all())

            bound_proposals, bound_ok = await guarded(
                session,
                unknowns,
                "source.link_bound_proposals",
                read_bound_proposals,
                [],
            )
            track("link_bound_proposals", bound_proposals, bound_ok, MAX_SOURCE_ROWS)
            new_pks = {
                p.id
                for p in bound_proposals
                if p.id not in {q.id for q in [*data.proposals, *data.link_proposals]}
            }
            if new_pks:

                async def read_bound_rungs() -> list[Any]:
                    stmt = (
                        select(OrderProposalRung)
                        .where(OrderProposalRung.proposal_pk.in_(new_pks))
                        .order_by(
                            OrderProposalRung.proposal_pk,
                            OrderProposalRung.rung_index,
                        )
                        .limit(MAX_SOURCE_ROWS * 4)
                    )
                    return list((await session.execute(stmt)).scalars().all())

                bound_rungs, bound_rungs_ok = await guarded(
                    session,
                    unknowns,
                    "source.link_bound_rungs",
                    read_bound_rungs,
                    [],
                )
                track(
                    "link_bound_rungs",
                    bound_rungs,
                    bound_rungs_ok,
                    MAX_SOURCE_ROWS * 4,
                )
                seen_rungs = {r.id for r in [*data.rungs, *data.link_rungs]}
                seen_proposals = {p.id for p in [*data.proposals, *data.link_proposals]}
                data.link_proposals += [
                    p for p in bound_proposals if p.id not in seen_proposals
                ]
                data.link_rungs += [r for r in bound_rungs if r.id not in seen_rungs]
        # Only the window's proposals show a candidate link; an order's decision
        # comes from the attempt rows or the rung's proposal column.
        decision_ids = [
            p.decision_id
            for p in data.proposals
            if getattr(p, "decision_id", None) is not None
        ]
        data.decisions = await load_decisions(session, unknowns, decision_ids)
        call_ids = sorted(
            {
                d.candidate_call_id
                for d in data.decisions.values()
                if d.candidate_call_id is not None
            },
            key=str,
        )
        if call_ids:

            async def read_ref_candidates() -> list[Any]:
                stmt = (
                    select(ScreenerPickLog)
                    .where(ScreenerPickLog.market == market.key)
                    .where(ScreenerPickLog.call_id.in_(call_ids))
                    .order_by(ScreenerPickLog.recorded_at, ScreenerPickLog.id)
                    .limit(MAX_SOURCE_ROWS)
                )
                return list((await session.execute(stmt)).scalars().all())

            found_picks, picks_ok = await guarded(
                session, unknowns, "source.ref_candidates", read_ref_candidates, []
            )
            if not picks_ok:
                data.incomplete.add("ref_candidates")
            elif len(found_picks) >= MAX_SOURCE_ROWS:
                data.incomplete.add("ref_candidates")
                unknowns.note("source.ref_candidates", "incomplete_evidence")
            wanted = set(call_ids)
            data.ref_candidates = [c for c in found_picks if c.call_id in wanted]

    fills, ok = await guarded(session, unknowns, "source.fills", read_fills, [])
    track("fills", fills, ok, MAX_SOURCE_ROWS)
    if ok:
        kept, dropped = supersede_provisional(fills)
        data.superseded = dropped
        data.evidence_fills = [r for r in kept if not is_opening_seed(r)]
        data.fills = [
            r for r in kept if lo <= r.filled_at < hi and not is_opening_seed(r)
        ]

    if include_lots and data.fills:
        fill_symbols = sorted({r.symbol.upper() for r in data.fills})

        async def history() -> list[Any]:
            return await read_history(session, market, hi, fill_symbols)

        rows, ok = await guarded(session, unknowns, "source.lots", history, [])
        track("lots", rows, ok, MAX_LEDGER_ROWS)
        if ok:
            data.history, _ = supersede_provisional(rows)

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
@dataclass(frozen=True, slots=True)
class RowLinks:
    """The optional link fields of a chain row (hk 4188).

    ``link_basis`` says how ``linked_ref`` was made; ``decision_ref`` is the
    decision the row descends from and ``decision_link_basis`` how that was
    made; ``link_reason`` is a closed code for a missing or weakened link.
    """

    link_basis: str | None = None
    decision_ref: str | None = None
    decision_link_basis: str | None = None
    link_reason: str | None = None

    def fields(self) -> dict[str, str | None]:
        return {
            "link_basis": self.link_basis,
            "decision_ref": self.decision_ref,
            "decision_link_basis": self.decision_link_basis,
            "link_reason": self.link_reason,
        }


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
    links: RowLinks | None = None,
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
    if links is not None:
        # Optional fields: present only with a value, so a row without link
        # evidence is exactly the pre-hk-4188 row.
        values.update({k: v for k, v in links.fields().items() if v is not None})
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


def _legacy_candidate(
    proposal: Any, candidates: list[Any]
) -> tuple[Any | None, str | None]:
    """The old inference, capped to the proposal's KST day (design Q20).

    A proposal without a stored decision gets the latest candidate of the same
    symbol recorded at or before it, but only one of the same KST day: an older
    candidate is not evidence of this proposal. Returns ``(candidate, reason)``.
    """
    earlier = [
        c
        for c in candidates
        if c.symbol.upper() == proposal.symbol.upper()
        and c.recorded_at <= proposal.created_at
    ]
    if not earlier:
        return None, "no_candidate_ref"
    day = kst_day(proposal.created_at)
    same_day = [c for c in earlier if kst_day(c.recorded_at) == day]
    if not same_day:
        return None, "candidate_outside_trading_day"
    return max(same_day, key=lambda c: (c.recorded_at, c.id)), None


def _candidate_link(
    proposal: Any, data: ChainData
) -> tuple[Any | None, str | None, str | None]:
    """``(candidate pick row, link basis, reason)`` of a proposal (§3 row 1).

    With a stored decision the candidate reference on it is the answer, labelled
    by the writer (``recorded`` from the session, ``inference`` from the
    server's same-day augmentation); the reader never infers on top of it.
    Without one, the capped legacy inference applies.
    """
    decision_id = getattr(proposal, "decision_id", None)
    if decision_id is None:
        candidate, reason = _legacy_candidate(proposal, data.candidates)
        return candidate, "inference" if candidate else None, reason
    decision = data.decisions.get(decision_id)
    if decision is None:
        return None, None, "candidate_ref_unresolved"
    if decision.candidate_call_id is None:
        return None, None, "no_candidate_ref"
    if "ref_candidates" in data.incomplete:
        # A failed or capped pick read can drop the writer's recorded pick,
        # so a surviving row is not evidence of which pick was meant.
        return None, None, "candidate_ref_unresolved"
    symbol = (decision.candidate_symbol or "").upper()
    named = [
        c
        for c in [*data.candidates, *data.ref_candidates]
        if c.call_id == decision.candidate_call_id and c.symbol.upper() == symbol
    ]
    if not named or decision.candidate_link_basis not in LINK_BASES:
        return None, None, "candidate_ref_unresolved"
    # Same pick the writer chose (latest row of the call and symbol).
    pick = max(named, key=lambda c: (c.recorded_at, c.id))
    return pick, decision.candidate_link_basis, None


def proposal_rung_rows(
    data: ChainData, market: MarketInfo
) -> list[tuple[datetime | None, Row]]:
    """Build proposal and rung rows."""
    out: list[tuple[datetime | None, Row]] = []
    rungs_by_proposal: dict[int, list[Any]] = defaultdict(list)
    for rung in data.rungs:
        rungs_by_proposal[rung.proposal_pk].append(rung)
    for p in data.proposals:
        rungs = rungs_by_proposal.get(p.id, [])
        candidate, candidate_basis, candidate_reason = _candidate_link(p, data)
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
                    links=RowLinks(
                        link_basis=candidate_basis,
                        decision_ref=decision_ref(p.decision_id)
                        if getattr(p, "decision_id", None) is not None
                        else None,
                        decision_link_basis="recorded"
                        if getattr(p, "decision_id", None) is not None
                        else None,
                        link_reason=candidate_reason,
                    ),
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
                        links=RowLinks(
                            link_basis="recorded",
                            decision_ref=decision_ref(p.decision_id)
                            if getattr(p, "decision_id", None) is not None
                            else None,
                            decision_link_basis="recorded"
                            if getattr(p, "decision_id", None) is not None
                            else None,
                        ),
                    ),
                )
            )
    return out


_INFERENCE_MARKER = "expired_inference:kis_regular_day_order_no_broker_original"


def reject_reason(*values: Any) -> str:
    text = " ".join(str(v or "").lower() for v in values)
    rules = (
        ("rate_limited", ("rate limit", "too many", "초당", "요청 횟수")),
        ("price_limit", ("price limit", "상한가", "하한가", "가격제한")),
        (
            "insufficient_balance",
            ("insufficient", "잔고 부족", "잔액 부족", "주문가능금액"),
        ),
        ("market_closed", ("market closed", "장 마감", "거래시간", "장운영시간")),
        (
            "invalid_order",
            ("invalid order", "invalid_order", "주문 오류", "주문수량 오류"),
        ),
    )
    return next(
        (reason for reason, terms in rules if any(term in text for term in terms)),
        "broker_other",
    )


def _day(broker: str, market: MarketInfo, when: datetime) -> Any:
    return order_trading_day("kis", market.instrument_type, when)


def _order_identity(order: Any, market: MarketInfo) -> OrderIdentity | None:
    number = getattr(order, "order_no", None) or getattr(order, "broker_order_id", None)
    if not number:
        return None
    return order_identity(
        SimpleNamespace(
            broker=order.broker,
            account_mode="live",
            instrument_type=market.instrument_type,
            symbol=order.symbol,
            side=order.side,
            currency=order.currency or market.currency,
            broker_order_id=number,
            filled_at=order.trade_date,
        )
    )


def _order_link(
    resolution: AttemptResolution,
    legacy: tuple[Any, bool] | None,
    rungs_by_binding: dict[tuple[Any, int], Any],
    proposals: dict[Any, Any],
) -> tuple[Any | None, RowLinks]:
    """The rung and link fields of one order (§3 row 4).

    The attempt rows decide when a live attempt exists: two decisions are
    ``ambiguous_attempts`` (no link at all, the legacy match included, since the
    key is shared), one decision with a ``sent`` outcome is ``recorded``, one
    without is ``inference`` (``attempt_unconfirmed``). With no live attempt the
    legacy rule applies: the rung's idempotency key equal to the ledger key is
    ``recorded``; a correlation-only match is ``inference``. The decision of a
    legacy match is the one stored on its proposal.
    """
    if resolution.kind == "unavailable":
        # The attempt read failed or was cut off: neither the attempts nor the
        # legacy rung match (which they could contradict) may be used.
        return None, RowLinks(link_reason="attempts_unavailable")
    if resolution.kind == "ambiguous":
        return None, RowLinks(link_reason="ambiguous_attempts")
    if resolution.kind in ("recorded", "unconfirmed"):
        basis = "recorded" if resolution.kind == "recorded" else "inference"
        bound = (
            rungs_by_binding.get((resolution.proposal_id, resolution.rung_index))
            if resolution.proposal_id is not None
            else None
        )
        return bound, RowLinks(
            link_basis=basis if bound is not None else None,
            decision_ref=decision_ref(resolution.decision_id),
            decision_link_basis=basis,
            link_reason="attempt_unconfirmed" if basis == "inference" else None,
        )
    if legacy is None:
        return None, RowLinks()
    rung, same_key = legacy
    basis = "recorded" if same_key else "inference"
    owner = proposals.get(rung.proposal_pk)
    stored = getattr(owner, "decision_id", None) if owner is not None else None
    return rung, RowLinks(
        link_basis=basis,
        decision_ref=decision_ref(stored) if stored is not None else None,
        decision_link_basis=basis if stored is not None else None,
    )


def order_rows(
    data: ChainData, market: MarketInfo
) -> tuple[list[tuple[datetime | None, Row]], dict[Any, str | None]]:
    out = []
    refs: dict[Any, str | None] = {}
    proposals = {p.id: p for p in [*data.proposals, *data.link_proposals]}
    linked_ids = {(source, order.id) for source, order in data.link_orders}
    totals: dict[Any, Decimal] = defaultdict(Decimal)
    for fill in data.evidence_fills:
        if fill.source in AUTHORITATIVE_SOURCES:
            fill_key = (order_identity(fill), _day(fill.broker, market, fill.filled_at))
            totals[fill_key] += Decimal(str(fill.filled_qty))
    rungs_by_binding: dict[tuple[Any, int], Any] = {}
    for rung in [*data.rungs, *data.link_rungs]:
        owner = proposals.get(rung.proposal_pk)
        if owner is not None and getattr(owner, "proposal_id", None) is not None:
            rungs_by_binding[(owner.proposal_id, rung.rung_index)] = rung
    rungs_by_identity: dict[Any, list[Any]] = defaultdict(list)
    for rung in [*data.rungs, *data.link_rungs]:
        proposal = proposals.get(rung.proposal_pk)
        if proposal is None:
            continue
        identity = rung_identity(proposal, rung, market, data.send_times)
        broker = _BROKER_BY_ACCOUNT_MODE.get(proposal.account_mode)
        sent = (
            data.send_times.get(rung.idempotency_key)
            if broker == "kis"
            else rung.created_at
        )
        if identity is not None and sent is not None and broker is not None:
            rungs_by_identity[(identity, _day(broker, market, sent))].append(rung)
    for source, order in [*data.orders, *data.link_orders]:
        identity = _order_identity(order, market)
        day = _day(order.broker, market, order.trade_date)
        key = (identity, day) if identity else None
        ref = make_ref("order", f"{source}:{order.id}")
        if key is not None:
            refs[key] = None if key in refs else ref
        if (source, order.id) in linked_ids:
            continue
        matches: list[tuple[Any, bool]] = []
        for candidate in rungs_by_identity.get(key, []):
            same_key = bool(
                getattr(candidate, "idempotency_key", None)
                and candidate.idempotency_key == order_send_key(order)
            )
            same_correlation = bool(
                getattr(candidate, "correlation_id", None)
                and candidate.correlation_id == getattr(order, "correlation_id", None)
            )
            if same_key or same_correlation:
                matches.append((candidate, same_key))
        rung, links = _order_link(
            data.order_links.resolve(order),
            matches[0] if len(matches) == 1 else None,
            rungs_by_binding,
            proposals,
        )
        total = totals.get(key, Decimal(0))
        quantity = Decimal(str(order.quantity)) if order.quantity is not None else None
        terminal = (
            order.status
            if order.status in {"filled", "rejected", "expired", "cancelled"}
            else None
        )
        reason = None
        if quantity and total == quantity:
            status, basis = "filled", "broker"
        elif quantity and 0 < total < quantity and not terminal:
            status, basis = "partially_filled", "broker"
        elif terminal:
            status, basis = terminal, "broker"
            if status == "rejected":
                reason = reject_reason(
                    getattr(order, "response_code", None),
                    getattr(order, "response_message", None),
                    getattr(order, "broker_status", None),
                )
            elif status == "expired":
                reason = "day_order_expired"
            elif status == "cancelled":
                reason = "cancelled"
        elif rung and getattr(rung, "void_reason", None) == _INFERENCE_MARKER:
            status, basis, reason = (
                "expired",
                "inference",
                "expired_inference_no_broker_original",
            )
        elif order.status in {"accepted", "pending", "partial"} and day == _day(
            order.broker, market, datetime.now(UTC)
        ):
            status, basis = "working", "broker"
        else:
            status, basis, reason = "unresolved", "unknown", "no_terminal_evidence"
        out.append(
            (
                order.trade_date,
                _row(
                    symbol=order.symbol,
                    stage="order",
                    event_at=order.trade_date,
                    event_ref=ref,
                    linked_ref=make_ref("rung", rung.id) if rung else None,
                    status=status,
                    reason=reason,
                    quantity=quantity,
                    price=order.price,
                    market=market,
                    currency=order.currency,
                    basis=basis,
                    links=links,
                ),
            )
        )
    return out, refs


def fill_rows(
    data: ChainData,
    market: MarketInfo,
    order_refs: dict[OrderIdentity, str | None],
) -> list[tuple[datetime | None, Row]]:
    out: list[tuple[datetime | None, Row]] = []
    for f in data.fills:
        order_ref = order_refs.get(
            (order_identity(f), _day(f.broker, market, f.filled_at))
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
                    # §3 row 5: the exact order identity (when it matched).
                    links=RowLinks(link_basis="recorded" if order_ref else None),
                ),
            )
        )
    return out


def _one_venue(
    bars: list[daily_bars.Bar], market: MarketInfo
) -> list[daily_bars.Bar] | None:
    """One venue's bars, so a session is counted once and one price is used.

    The daily tables are keyed by (symbol, venue); KR can hold both KRX and NTX
    rows for a date. KRX is the primary venue; any other multi-venue symbol is
    ambiguous (``None``), never an arbitrary pick.
    """
    venues = {b.venue for b in bars}
    if len(venues) <= 1:
        return bars
    if market.key == "kr" and "KRX" in venues:
        return [b for b in bars if b.venue == "KRX"]
    return None


def markout_rows(
    data: ChainData, market: MarketInfo
) -> list[tuple[datetime | None, Row]]:
    """Close of the ``MARKOUT_SESSIONS``-th bar strictly after each fill.

    A fill without that bar still gets a row: the missing bar is the unknown.
    """
    by_symbol: dict[str, list[daily_bars.Bar]] = defaultdict(list)
    for bar in data.bars:
        by_symbol[bar.symbol.upper()].append(bar)
    series: dict[str, list[daily_bars.Bar] | None] = {}
    for symbol, found in by_symbol.items():
        chosen = _one_venue(found, market)
        series[symbol] = (
            sorted(chosen, key=lambda b: b.time) if chosen is not None else None
        )
    out: list[tuple[datetime | None, Row]] = []
    for f in data.fills:
        symbol = f.symbol.upper()
        bars = series.get(symbol, [])
        mixed = bars is None
        later = [b for b in bars or [] if b.time > f.filled_at]
        horizon = (
            later[MARKOUT_SESSIONS - 1] if len(later) >= MARKOUT_SESSIONS else None
        )
        miss = "source_mixed" if mixed else "missing_data"
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
                    reason=None if horizon else miss,
                    quantity=f.filled_qty,
                    price=horizon.close if horizon else None,
                    market=market,
                    currency=f.currency,
                    # A markout is computed from its own fill (same id).
                    links=RowLinks(link_basis="recorded"),
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
                    # §3 row 6: the position is a FIFO reading over many fills.
                    links=RowLinks(link_basis="inference"),
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
    rows += proposal_rung_rows(data, market)
    orders, order_refs = order_rows(data, market)
    rows += orders
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
    times += [o.trade_date for _, o in data.orders]
    aware = [t for t in times if t is not None and t.tzinfo is not None]
    return max(aware) if aware else None
