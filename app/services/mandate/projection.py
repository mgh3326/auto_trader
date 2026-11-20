"""Fill projection of the mandate plans (hk 3795 T4b): ledger fills -> plan events.

Reads the plan's bound proposals, their rungs and the authoritative ledger fills
the sleeve attribution (T1m-a) ties to those rungs, folds that into
``projection_rules.project`` and appends the events through the T1 store, the only
writer. The ledger and the order tables are read, never written (hard rule 5).

Holdings are an input: the caller passes ``HoldingsReading`` rows (an operator's
broker read). This module makes no broker call.

Locks follow §4.6: the symbol lock (L1) before the sleeve lock (L3), taken before
the plan is read, so a concurrent ``released`` or registration cannot interleave.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.mandate import MandatePlan, MandatePlanEvent, MandatePlanTranche
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.strategy_sleeves import StrategySleeve
from app.services.mandate import fence_locks
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.mandate.fold import PlanState
from app.services.mandate.locks import acquire_in_order
from app.services.mandate.projection_rules import (
    ACTOR,
    DEFAULT_MAX_HOLDINGS_AGE,
    FillFact,
    Hold,
    HoldingsReading,
    PlanFacts,
    PlannedEvent,
    ProjectionPlan,
    ProposalFact,
    RungFact,
    project,
)
from app.services.order_proposals.state_machine import is_terminal
from app.services.strategy_sleeves import store as sleeve_store

_US_EASTERN = ZoneInfo("America/New_York")
ZERO = Decimal("0")


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def trade_date_of(filled_at: datetime, market: str) -> date:
    """The market's trading date of a fill (KR: the KST day; US: the 20:00 ET roll)."""
    when = filled_at if filled_at.tzinfo else filled_at.replace(tzinfo=UTC)
    if market == "equity_us":
        from app.services.execution_ledger.kis_lots import us_trading_day_window

        return us_trading_day_window(when)[1].astimezone(_US_EASTERN).date()
    return when.astimezone(KST).date()


@dataclass(frozen=True)
class PlanReport:
    plan_id: int
    symbol: str
    authority: str | None
    cleanup: str
    planned: tuple[PlannedEvent, ...]
    holds: tuple[Hold, ...]
    #: ``True`` once the events were committed (never in a preview)
    written: bool = False
    error: str | None = None
    final_authority: str | None = None
    final_cleanup: str | None = None
    appended_seqs: tuple[int, ...] = field(default_factory=tuple)


async def _recovered_orders(
    session: AsyncSession,
    sleeve: StrategySleeve,
    state: PlanState,
    proposals: Sequence[OrderProposal],
    rungs: Sequence[OrderProposalRung],
    plan: MandatePlan,
) -> dict[int, Any]:
    """Orders a rolled-back human approval left without a number on the rung (X26).

    A committed, unreleased fence carries the deterministic send key of the order it
    allowed. When the rung itself has neither an order number nor a key (the callback
    transaction rolled back after the broker accepted), the Kiwoom mock ledger's place
    row for that key ties the order back; no row means no evidence and nothing changes.
    """
    open_fences = {
        f.proposal_id: f for f in state.fences if f.seq not in state.released_fence_seqs
    }
    if not open_fences:
        return {}
    keys: dict[int, str] = {}
    for event in await ms.list_plan_events(session, plan.id):
        if event.event != "tranche_send_fenced" or event.seq not in {
            f.seq for f in open_fences.values()
        }:
            continue
        key = (event.evidence or {}).get(v.SEND_KEY)
        if isinstance(key, str) and key.strip():
            keys[event.seq] = key.strip()
    candidates = []
    for proposal in proposals:
        fence_ = open_fences.get(proposal.proposal_id)
        key = keys.get(fence_.seq) if fence_ is not None else None
        mine = [r for r in rungs if r.proposal_pk == proposal.id]
        if key is None or len(mine) != 1:
            continue
        rung = mine[0]
        if (rung.broker_order_id or "").strip() or (rung.idempotency_key or "").strip():
            continue
        if is_terminal(rung.state):
            continue
        candidates.append(sleeve_store.RecoveredKiwoomCandidate(rung, proposal, key))
    return await sleeve_store.recover_kiwoom_orders(session, sleeve, candidates)


def _with_recovered_order(
    fact: RungFact, order: Any, market: str
) -> tuple[RungFact, list[FillFact]]:
    """The rung as the recovered order proves it: its number and key, its fills.

    Only a fully booked rung is settled (``filled``); anything less stays open, because
    a resting or part-filled order is not provably over.
    """
    fills = [
        FillFact(
            ledger_id=f.ledger_id,
            rung_id=f.rung_id,
            quantity=f.quantity,
            price=f.price,
            trade_date=trade_date_of(f.filled_at, market),
            filled_at=f.filled_at
            if f.filled_at.tzinfo
            else f.filled_at.replace(tzinfo=UTC),
        )
        for f in order.fills
        if f.side == "buy"
    ]
    booked = sum((f.quantity for f in fills), ZERO)
    filled = booked >= fact.quantity > ZERO
    return (
        replace(
            fact,
            broker_order_id=order.order_id,
            state="filled" if filled else fact.state,
            filled_qty=booked if filled else fact.filled_qty,
        ),
        fills,
    )


async def _proposal_facts(
    session: AsyncSession,
    plan: MandatePlan,
    sleeve: StrategySleeve,
    state: PlanState,
) -> tuple[ProposalFact, ...]:
    stream_ids = [pid for _k, pid in state.proposals]
    clauses = [OrderProposal.mandate_plan_id == plan.id]
    if stream_ids:
        clauses.append(OrderProposal.proposal_id.in_(stream_ids))
    proposals = list(
        (
            await session.execute(
                select(OrderProposal).where(or_(*clauses)).order_by(OrderProposal.id)
            )
        )
        .scalars()
        .all()
    )
    if not proposals:
        return ()
    rungs = list(
        (
            await session.execute(
                select(OrderProposalRung)
                .where(OrderProposalRung.proposal_pk.in_([p.id for p in proposals]))
                .order_by(OrderProposalRung.id)
            )
        )
        .scalars()
        .all()
    )
    inputs = await sleeve_store.load_sleeve_inputs(session, sleeve)
    fills_by_rung: dict[int, list[FillFact]] = {}
    for fill in inputs.fills:
        if fill.side != "buy":
            continue
        fills_by_rung.setdefault(fill.rung_id, []).append(
            FillFact(
                ledger_id=fill.ledger_id,
                rung_id=fill.rung_id,
                quantity=fill.quantity,
                price=fill.price,
                trade_date=trade_date_of(fill.filled_at, sleeve.market),
                filled_at=fill.filled_at
                if fill.filled_at.tzinfo
                else fill.filled_at.replace(tzinfo=UTC),
            )
        )
    unattributed = frozenset(inputs.unattributed_rungs)
    recovered = await _recovered_orders(session, sleeve, state, proposals, rungs, plan)
    facts: list[ProposalFact] = []
    for proposal in proposals:
        mine = [r for r in rungs if r.proposal_pk == proposal.id]
        rung_facts = []
        extra_fills: list[FillFact] = []
        for r in mine:
            fact = RungFact(
                rung_id=r.id,
                state=r.state,
                quantity=_dec(r.quantity) or ZERO,
                limit_price=_dec(r.limit_price),
                filled_qty=_dec(r.filled_qty),
                broker_order_id=(r.broker_order_id or "").strip() or None,
                idempotency_key=(r.idempotency_key or "").strip() or None,
            )
            order = recovered.get(r.id)
            if order is not None:
                fact, more = _with_recovered_order(fact, order, sleeve.market)
                extra_fills.extend(more)
            rung_facts.append(fact)
        rung_facts = tuple(rung_facts)
        facts.append(
            ProposalFact(
                proposal_id=proposal.proposal_id,
                rungs=rung_facts,
                fills=tuple(
                    [fill for r in mine for fill in fills_by_rung.get(r.id, [])]
                    + extra_fills
                ),
                unattributed_rung_ids=frozenset(r.id for r in mine) & unattributed,
            )
        )
    return tuple(facts)


async def load_plan_facts(
    session: AsyncSession,
    plan: MandatePlan,
    sleeve: StrategySleeve,
    state: PlanState,
    *,
    holdings: Sequence[HoldingsReading],
    now: datetime,
    max_holdings_age: timedelta = DEFAULT_MAX_HOLDINGS_AGE,
) -> PlanFacts:
    tranches = list(
        (
            await session.execute(
                select(MandatePlanTranche)
                .where(MandatePlanTranche.plan_id == plan.id)
                .order_by(MandatePlanTranche.tranche_index)
            )
        )
        .scalars()
        .all()
    )
    amounts = tuple(Decimal(str(t.amount)) for t in tranches)
    if len(amounts) != v.MAX_TRANCHES:
        raise ms.MandateIntegrityError(
            f"plan {plan.id} has {len(amounts)} tranche rows, expected {v.MAX_TRANCHES}"
        )
    return PlanFacts(
        plan_account_ref=plan.account_ref,
        scope_account_refs=tuple(plan.scope_account_refs),
        symbol=sleeve_store.sleeve_symbol(plan.symbol, sleeve.market),
        tranche_amounts=(amounts[0], amounts[1], amounts[2]),
        proposals=await _proposal_facts(session, plan, sleeve, state),
        holdings=tuple(
            HoldingsReading(
                account_ref=r.account_ref,
                symbol=sleeve_store.sleeve_symbol(r.symbol, sleeve.market),
                quantity=r.quantity,
                checked_at=r.checked_at,
            )
            for r in holdings
        ),
        now=now,
        max_holdings_age=max_holdings_age,
    )


async def unreleased_plan_ids(
    session: AsyncSession, *, sleeve_id: int | None = None
) -> list[int]:
    stmt = (
        select(MandatePlan.id)
        .where(
            ~select(MandatePlanEvent.id)
            .where(
                MandatePlanEvent.plan_id == MandatePlan.id,
                MandatePlanEvent.event == "released",
            )
            .exists()
        )
        .order_by(MandatePlan.id)
    )
    if sleeve_id is not None:
        stmt = stmt.where(MandatePlan.sleeve_id == sleeve_id)
    return [int(i) for i in (await session.execute(stmt)).scalars().all()]


async def project_plan(
    session: AsyncSession,
    plan_id: int,
    *,
    holdings: Sequence[HoldingsReading] = (),
    now: datetime | None = None,
    max_holdings_age: timedelta = DEFAULT_MAX_HOLDINGS_AGE,
    write: bool = False,
) -> PlanReport:
    """Project one plan. With ``write`` the events are appended (the caller commits).

    The plan's locks are taken first (L1 symbol, L3 sleeve) so the state read here
    is the state the events are appended to. A refusal from the store ends the plan
    with ``error`` set and appends nothing further; the caller rolls the plan back.
    """
    clock = now or datetime.now(UTC)
    plan = await ms.get_plan(session, plan_id)
    if plan is None:
        raise ms.MandateError(v.MandateRefusal.UNSUPPORTED, f"plan {plan_id} not found")
    sleeve = await sleeve_store.get_sleeve(session, plan.sleeve_id)
    if sleeve is None:
        raise ms.MandateIntegrityError(f"plan {plan_id} has no sleeve")
    realm, market = ms.realm_and_market(sleeve)
    await acquire_in_order(
        session,
        realm=realm,
        market=market,
        symbol=plan.symbol,
        sleeve_id=sleeve.id,
    )
    state = await ms.plan_state(session, plan_id)
    facts = await load_plan_facts(
        session,
        plan,
        sleeve,
        state,
        holdings=holdings,
        now=clock,
        max_holdings_age=max_holdings_age,
    )
    projection: ProjectionPlan = project(state, facts)
    base = PlanReport(
        plan_id=plan.id,
        symbol=plan.symbol,
        authority=state.authority,
        cleanup=state.cleanup,
        planned=projection.events,
        holds=projection.holds,
        final_authority=projection.final_state.authority,
        final_cleanup=projection.final_state.cleanup,
    )
    if not write or not projection.events:
        return base
    # hk 3795 §4.6 / X26: the events that close a fence and set its durable exposure
    # (tranche_filled / _partially_filled / _unfilled, and the rest of this write) go in
    # under F3s, the lock a send fence and its release take, so a fence and its closing
    # event never interleave. Caller locks (L1, L3) are already held, as for any writer.
    await fence_locks.lock_mutex(
        session, fence_locks.sleeve_mutex_key(sleeve.id), "sleeve"
    )
    seqs: list[int] = []
    for planned in projection.events:
        try:
            row = await ms.append_plan_event(
                session,
                plan_id=plan.id,
                event=planned.event,
                actor=ACTOR,
                evidence=planned.evidence,
                **planned.fields,
            )
        except ms.MandateError as exc:
            return replace(base, error=str(exc), appended_seqs=tuple(seqs))
        seqs.append(row.seq)
    return replace(base, appended_seqs=tuple(seqs))


__all__ = [
    "PlanReport",
    "load_plan_facts",
    "project_plan",
    "trade_date_of",
    "unreleased_plan_ids",
]
