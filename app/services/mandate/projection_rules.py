"""Pure rules of the fill projection (hk 3795 T4b, design §3.2 / §3.7 / §8).

Given a plan's folded state and the facts read from the order and ledger tables,
``project`` returns the plan events the projection CLI would append and the closed
reasons it could not append the rest. No database, no clock, no broker.

Hard rules kept here:

* I10: a tranche event is written only from **authoritative** ledger fills the
  loader attributed to the plan's own rungs. A rung that reports a fill the ledger
  has not booked (websocket-only rows are not authoritative) holds the proposal
  open (``fill_not_booked``); nothing is guessed.
* I19: ``released`` needs settled orders, no open send fence, and a holdings
  disposal. A ``zero_holdings`` proof must read **exactly** the accounts fixed in
  ``scope_account_refs`` at registration, each with a fresh, later-than-last-fill,
  zero reading. Any other account reference proves nothing.
* ``closed`` (after ``thesis_broken``) needs every bought order settled and the
  plan account's holdings at zero.
* Every event is applied to the fold before it is returned, so the plan this
  function proposes is a stream the store and the DB trigger accept.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from app.services.mandate.fold import (
    InvalidPlanEvent,
    PlanEvent,
    PlanState,
    apply_event,
)
from app.services.order_proposals.state_machine import is_terminal

ZERO = Decimal("0")
ACTOR = "system:mandate_projection"
#: §3.2 "회차 완료": a tranche is complete from 95% of its amount.
COMPLETE_RATIO = Decimal("0.95")
#: ``numeric(28, 12)`` columns: amounts are rounded once, here, before they are stored.
_Q12 = Decimal("0.000000000001")
#: A reading stamped later than ``now`` by more than this is not a reading.
CLOCK_SKEW = timedelta(minutes=5)
DEFAULT_MAX_HOLDINGS_AGE = timedelta(hours=6)


class ProjectionHold(StrEnum):
    """Why an event was not written (closed vocabulary, reported by the CLI)."""

    PROPOSAL_ROW_MISSING = "proposal_row_missing"
    RUNG_OPEN = "rung_open"
    OUTCOME_UNKNOWN = "outcome_unknown"
    FILL_NOT_BOOKED = "fill_not_booked"
    FILL_ATTRIBUTION_UNKNOWN = "fill_attribution_unknown"
    FENCE_UNCLOSED = "fence_unclosed"
    BOUND_PROPOSAL_NOT_IN_STREAM = "bound_proposal_not_in_stream"
    RECORDED_FILL_MISMATCH = "recorded_fill_mismatch"
    SCOPE_ACCOUNT_UNREAD = "scope_account_unread"
    HOLDINGS_STALE = "holdings_stale"
    HOLDINGS_REMAIN = "holdings_remain"
    HOLDINGS_UNKNOWN = "holdings_unknown"
    EVENT_REFUSED = "event_refused"


@dataclass(frozen=True)
class Hold:
    code: ProjectionHold
    proposal_id: UUID | None = None
    tranche_index: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class RungFact:
    rung_id: int
    state: str
    quantity: Decimal
    limit_price: Decimal | None
    #: the broker-reported cumulative fill the rung row carries (a hint, never proof)
    filled_qty: Decimal | None
    broker_order_id: str | None
    idempotency_key: str | None

    @property
    def sent_evidence(self) -> bool:
        return bool(self.broker_order_id or self.idempotency_key)


@dataclass(frozen=True)
class FillFact:
    """One authoritative ledger fill attributed to a rung of the plan."""

    ledger_id: int
    rung_id: int
    quantity: Decimal
    price: Decimal
    trade_date: date
    filled_at: datetime


@dataclass(frozen=True)
class ProposalFact:
    proposal_id: UUID
    rungs: tuple[RungFact, ...]
    fills: tuple[FillFact, ...] = ()
    #: rungs whose order number carries ledger fills the loader could not attribute
    unattributed_rung_ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class HoldingsReading:
    """A holdings read of one account for one symbol, supplied by the operator."""

    account_ref: str
    symbol: str
    quantity: Decimal
    checked_at: datetime


@dataclass(frozen=True)
class PlanFacts:
    plan_account_ref: str
    scope_account_refs: tuple[str, ...]
    symbol: str
    #: the three tranche amounts, in tranche order
    tranche_amounts: tuple[Decimal, Decimal, Decimal]
    #: every proposal bound to the plan (``order_proposals.mandate_plan_id``)
    proposals: tuple[ProposalFact, ...]
    holdings: tuple[HoldingsReading, ...]
    now: datetime
    max_holdings_age: timedelta = DEFAULT_MAX_HOLDINGS_AGE


@dataclass(frozen=True)
class PlannedEvent:
    event: str
    fields: dict[str, Any]
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProjectionPlan:
    events: tuple[PlannedEvent, ...]
    holds: tuple[Hold, ...]
    final_state: PlanState


# ----------------------------------------------------------------------- amounts
def q12(value: Decimal) -> Decimal:
    return value.quantize(_Q12, rounding=ROUND_HALF_UP)


def fills_total(fills: tuple[FillFact, ...]) -> tuple[Decimal, Decimal]:
    """(amount, quantity) of fills, each rounded to the column scale once."""
    amount = q12(sum((f.price * f.quantity for f in fills), ZERO))
    quantity = q12(sum((f.quantity for f in fills), ZERO))
    return amount, quantity


# ----------------------------------------------------------------------- rungs
def rung_status(rung: RungFact) -> str:
    """``settled`` / ``open`` / ``unknown``: what the rung row alone proves."""
    if rung.state == "unverified":
        return "unknown"
    if rung.state == "voided_local_stale" and rung.sent_evidence:
        return "unknown"
    if is_terminal(rung.state):
        return "settled"
    return "open"


def _expected_quantity(rung: RungFact) -> Decimal:
    if rung.filled_qty is not None:
        return rung.filled_qty
    return rung.quantity if rung.state == "filled" else ZERO


@dataclass(frozen=True)
class ProposalOutcome:
    fill_amount: Decimal
    fill_quantity: Decimal
    ledger_ids: tuple[int, ...]
    trade_date: date | None
    one_share_value: Decimal | None


def proposal_outcome(
    proposal: ProposalFact, *, unclosed_fence: bool, tranche_index: int | None = None
) -> ProposalOutcome | Hold:
    """What a proposal's orders ended as, or why that is not provable yet."""

    def hold(code: ProjectionHold, detail: str = "") -> Hold:
        return Hold(code, proposal.proposal_id, tranche_index, detail)

    if not proposal.rungs:
        return hold(ProjectionHold.RUNG_OPEN, "the proposal has no rung")
    statuses = {rung_status(r) for r in proposal.rungs}
    if "open" in statuses:
        return hold(ProjectionHold.RUNG_OPEN)
    if "unknown" in statuses:
        return hold(ProjectionHold.OUTCOME_UNKNOWN)
    rung_ids = {r.rung_id for r in proposal.rungs}
    if proposal.unattributed_rung_ids & rung_ids:
        return hold(ProjectionHold.FILL_ATTRIBUTION_UNKNOWN)
    if unclosed_fence and not any(r.broker_order_id for r in proposal.rungs):
        # A committed send fence with no broker order number on any rung: the call
        # may have gone out and the order number been lost (X26). Absence of a
        # number does not prove an unsent order.
        return hold(ProjectionHold.FENCE_UNCLOSED)
    booked = {
        r.rung_id: sum(
            (f.quantity for f in proposal.fills if f.rung_id == r.rung_id), ZERO
        )
        for r in proposal.rungs
    }
    for rung in proposal.rungs:
        if booked[rung.rung_id] < _expected_quantity(rung):
            return hold(
                ProjectionHold.FILL_NOT_BOOKED,
                "the rung reports a fill the authoritative ledger has not booked",
            )
    amount, quantity = fills_total(proposal.fills)
    prices = [r.limit_price for r in proposal.rungs if r.limit_price is not None]
    return ProposalOutcome(
        fill_amount=amount,
        fill_quantity=quantity,
        ledger_ids=tuple(sorted(f.ledger_id for f in proposal.fills)),
        trade_date=max((f.trade_date for f in proposal.fills), default=None),
        one_share_value=max(prices) if prices else None,
    )


def tranche_event_for(
    outcome: ProposalOutcome, *, tranche_amount: Decimal, prior_amount: Decimal
) -> str:
    """The terminal event of a closed proposal (§3.2 "회차 기준값").

    Complete from 95% of the tranche amount, or when something is filled and the
    remainder is below one share at the proposal's limit price.
    """
    if outcome.fill_quantity == 0:
        return "tranche_unfilled"
    cumulative = prior_amount + outcome.fill_amount
    if cumulative >= tranche_amount * COMPLETE_RATIO:
        return "tranche_filled"
    remainder = tranche_amount - cumulative
    if outcome.one_share_value is not None and remainder < outcome.one_share_value:
        return "tranche_filled"
    return "tranche_partially_filled"


# ----------------------------------------------------------------------- holdings
def _normalize_symbol(symbol: str) -> str:
    return str(symbol).strip().upper()


def _latest_reading(
    readings: tuple[HoldingsReading, ...], account_ref: str, symbol: str
) -> HoldingsReading | None | str:
    """The newest reading of one account and symbol; ``"conflict"`` if two newest differ."""
    mine = [
        r
        for r in readings
        if r.account_ref == account_ref and _normalize_symbol(r.symbol) == symbol
    ]
    if not mine:
        return None
    newest = max(r.checked_at for r in mine)
    latest = [r for r in mine if r.checked_at == newest]
    if len({r.quantity for r in latest}) != 1:
        return "conflict"
    return latest[0]


def zero_holdings_proof(
    facts: PlanFacts, accounts: tuple[str, ...], *, last_fill_at: datetime | None
) -> tuple[dict[str, Any] | None, Hold | None]:
    """Evidence that every account in ``accounts`` holds none of the symbol.

    ``accounts`` is ``scope_account_refs`` for a disposal and the plan account alone
    for ``closed``. A reading counts only for an account reference in that tuple.
    """
    symbol = _normalize_symbol(facts.symbol)
    checked: list[HoldingsReading] = []
    unread: list[str] = []
    for ref in accounts:
        reading = _latest_reading(facts.holdings, ref, symbol)
        if reading is None:
            unread.append(ref)
            continue
        if reading == "conflict":
            return None, Hold(
                ProjectionHold.HOLDINGS_UNKNOWN, detail="conflicting readings"
            )
        assert isinstance(reading, HoldingsReading)
        checked.append(reading)
    if unread:
        return None, Hold(
            ProjectionHold.SCOPE_ACCOUNT_UNREAD,
            detail=f"{len(unread)} account(s) of the plan scope were not read",
        )
    for reading in checked:
        if not reading.quantity.is_finite() or reading.quantity < 0:
            return None, Hold(
                ProjectionHold.HOLDINGS_UNKNOWN, detail="unreadable quantity"
            )
        if reading.checked_at > facts.now + CLOCK_SKEW:
            return None, Hold(
                ProjectionHold.HOLDINGS_UNKNOWN, detail="reading is in the future"
            )
        if facts.now - reading.checked_at > facts.max_holdings_age:
            return None, Hold(ProjectionHold.HOLDINGS_STALE)
        if last_fill_at is not None and reading.checked_at < last_fill_at:
            return None, Hold(
                ProjectionHold.HOLDINGS_STALE,
                detail="the reading is older than the plan's last fill",
            )
    if any(r.quantity != 0 for r in checked):
        return None, Hold(ProjectionHold.HOLDINGS_REMAIN)
    return (
        {
            "holdings_quantity": "0",
            "holdings_checked_at": min(r.checked_at for r in checked).isoformat(),
            "holdings_account_refs": sorted(accounts),
        },
        None,
    )


# ----------------------------------------------------------------------- the plan
def project(state: PlanState, facts: PlanFacts) -> ProjectionPlan:
    """The events to append to ``state``'s stream, and what blocks the others."""
    events: list[PlannedEvent] = []
    holds: list[Hold] = []
    current = state
    if state.authority is None or state.released:
        return ProjectionPlan((), (), state)

    def emit(name: str, fields: dict[str, Any], evidence: dict[str, Any]) -> bool:
        nonlocal current
        candidate = PlanEvent(
            event=name,
            seq=current.last_seq + 1,
            evidence=dict(evidence),
            actor=ACTOR,
            **fields,
        )
        try:
            current = apply_event(current, candidate)
        except InvalidPlanEvent as exc:
            holds.append(
                Hold(
                    ProjectionHold.EVENT_REFUSED,
                    tranche_index=fields.get("tranche_index"),
                    detail=f"{name}: {exc.code.value}",
                )
            )
            return False
        events.append(PlannedEvent(name, dict(fields), dict(evidence)))
        return True

    by_id = {p.proposal_id: p for p in facts.proposals}
    last_fill_at = max(
        (f.filled_at for p in facts.proposals for f in p.fills), default=None
    )

    # 1. Tranche terminal events, in stream order (I10).
    for tranche_index, proposal_id in state.proposals:
        if proposal_id in current.closed_proposals:
            continue
        proposal = by_id.get(proposal_id)
        if proposal is None:
            holds.append(
                Hold(ProjectionHold.PROPOSAL_ROW_MISSING, proposal_id, tranche_index)
            )
            continue
        unclosed = any(
            f.proposal_id == proposal_id and f.seq not in current.released_fence_seqs
            for f in current.fences
        )
        outcome = proposal_outcome(
            proposal, unclosed_fence=unclosed, tranche_index=tranche_index
        )
        if isinstance(outcome, Hold):
            holds.append(outcome)
            continue
        tranche = current.tranche(tranche_index)
        name = tranche_event_for(
            outcome,
            tranche_amount=facts.tranche_amounts[tranche_index - 1],
            prior_amount=tranche.filled_amount,
        )
        fields: dict[str, Any] = {
            "tranche_index": tranche_index,
            "proposal_id": proposal_id,
        }
        evidence: dict[str, Any] = {}
        if name == "tranche_unfilled":
            evidence["reason_code"] = "zero_fill_terminal"
        else:
            fields["fill_amount"] = outcome.fill_amount
            fields["fill_quantity"] = outcome.fill_quantity
            evidence["ledger_row_ids"] = list(outcome.ledger_ids)
            if outcome.trade_date is not None:
                evidence["trade_date"] = outcome.trade_date.isoformat()
            if name == "tranche_filled" and tranche_index == 1:
                fields["first_fill_amount"] = q12(
                    tranche.filled_amount + outcome.fill_amount
                )
                fields["first_fill_quantity"] = q12(
                    tranche.filled_quantity + outcome.fill_quantity
                )
        emit(name, fields, evidence)

    # 2. Authority steps the fills imply.
    if current.authority == "approved" and current.tranche(1).completed:
        emit("first_tranche_settled", {}, {"reason_code": "first_tranche_completed"})
    if current.authority == "active" and current.tranche(3).completed:
        emit("exhausted", {}, {"reason_code": "third_tranche_completed"})

    # 3. What the orders say about the plan as a whole.
    stream_proposals = {pid for _k, pid in current.proposals}
    blockers: list[Hold] = []
    for proposal in facts.proposals:
        statuses = {rung_status(r) for r in proposal.rungs}
        if "open" in statuses:
            blockers.append(Hold(ProjectionHold.RUNG_OPEN, proposal.proposal_id))
        elif "unknown" in statuses:
            blockers.append(Hold(ProjectionHold.OUTCOME_UNKNOWN, proposal.proposal_id))
        if proposal.proposal_id not in stream_proposals and any(
            r.sent_evidence for r in proposal.rungs
        ):
            blockers.append(
                Hold(ProjectionHold.BOUND_PROPOSAL_NOT_IN_STREAM, proposal.proposal_id)
            )
    if current.unclosed_fences:
        blockers.append(Hold(ProjectionHold.FENCE_UNCLOSED))
    # Fills of a proposal the stream has already closed must equal what was recorded;
    # a late fill after a terminal event cannot be appended (one terminal event per
    # proposal), so it blocks cleanup and closing until an operator looks at it.
    ledger_amount = q12(
        sum(
            (
                fills_total(p.fills)[0]
                for p in facts.proposals
                if p.proposal_id in current.closed_proposals
            ),
            ZERO,
        )
    )
    recorded_amount = q12(sum((t.filled_amount for t in current.tranches), ZERO))
    if ledger_amount != recorded_amount:
        blockers.append(
            Hold(
                ProjectionHold.RECORDED_FILL_MISMATCH,
                detail="the ledger's fills for this plan differ from the recorded tranche fills",
            )
        )
    holds.extend(h for h in blockers if h not in holds)
    orders_settled = not blockers and not any(
        t.open_proposals for t in current.tranches
    )

    # 4. thesis_broken -> closed: orders settled and the plan account holds nothing.
    if current.authority == "thesis_broken" and orders_settled:
        proof, hold = zero_holdings_proof(
            facts, (facts.plan_account_ref,), last_fill_at=last_fill_at
        )
        if proof is None:
            assert hold is not None
            holds.append(hold)
        else:
            emit(
                "closed",
                {},
                {**proof, "open_order_count": 0, "unknown_order_count": 0},
            )

    # 5. Cleanup (§3.7): orders_settled -> holdings_disposed -> released.
    scope_proof: dict[str, Any] | None = None
    scope_hold: Hold | None = None
    if current.authority in ("rejected", "retired", "closed", "exhausted"):
        if current.ever_approved:
            scope_proof, scope_hold = zero_holdings_proof(
                facts, facts.scope_account_refs, last_fill_at=last_fill_at
            )
        # An exhausted plan holds shares by design: it is cleaned up only once a
        # scope-wide zero reading says nothing is left.
        eligible = current.authority != "exhausted" or scope_proof is not None
        if current.authority == "exhausted" and scope_proof is None and scope_hold:
            holds.append(scope_hold)
        if eligible and orders_settled and current.cleanup in ("none", "winding_down"):
            emit(
                "orders_settled",
                {},
                {
                    "reason_code": "orders_settled",
                    "open_order_count": 0,
                    "unknown_order_count": 0,
                    "ledger_row_ids": sorted(
                        f.ledger_id for p in facts.proposals for f in p.fills
                    ),
                },
            )
        if eligible and current.cleanup == "orders_clear":
            if not current.ever_approved:
                emit(
                    "holdings_disposed",
                    {},
                    {"holdings_disposition": "never_adopted"},
                )
            elif scope_proof is not None:
                emit(
                    "holdings_disposed",
                    {},
                    {**scope_proof, "holdings_disposition": "zero_holdings"},
                )
            elif scope_hold is not None and current.authority != "exhausted":
                holds.append(scope_hold)
        if current.cleanup == "disposable":
            emit("released", {}, {"reason_code": "cleanup_complete"})

    return ProjectionPlan(tuple(events), tuple(holds), current)


__all__ = [
    "ACTOR",
    "COMPLETE_RATIO",
    "DEFAULT_MAX_HOLDINGS_AGE",
    "FillFact",
    "Hold",
    "HoldingsReading",
    "PlanFacts",
    "PlannedEvent",
    "ProjectionHold",
    "ProjectionPlan",
    "ProposalFact",
    "ProposalOutcome",
    "RungFact",
    "fills_total",
    "project",
    "proposal_outcome",
    "q12",
    "rung_status",
    "tranche_event_for",
    "zero_holdings_proof",
]
