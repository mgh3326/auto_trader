"""Fold of a plan's event stream into its state (hk 3795 T1, §3.2).

A plan row is immutable; its state is the fold of its append-only events. The
last event's name is not the state (an ``approved`` plan stays ``approved``
through ``terms_rebound`` and ``resumed``). Gates, triggers' Python twins and
the operator tools all read state through ``fold_plan_events``, and the store
validates a new event with ``apply_event`` before it writes it.

Pure: standard library plus ``vocabulary``. No database, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.services.mandate import vocabulary as v

ZERO = Decimal("0")


class InvalidPlanEvent(ValueError):
    """An event the stream cannot accept; ``code`` is a ``MandateRefusal`` value."""

    def __init__(self, code: v.MandateRefusal, message: str) -> None:
        super().__init__(f"{code.value}: {message}")
        self.code = code


@dataclass(frozen=True)
class PlanEvent:
    """One row of ``review.mandate_plan_events`` (the columns the fold reads)."""

    event: str
    seq: int = 0
    tranche_index: int | None = None
    terms_id: int | None = None
    terms_sha256: str | None = None
    effective_session: date | None = None
    resume_target: str | None = None
    proposal_id: UUID | None = None
    notional: Decimal | None = None
    kst_day: date | None = None
    approval_mode: str | None = None
    account_ref: str | None = None
    attempt_id: UUID | None = None
    fenced_seq: int | None = None
    thesis_seq: int | None = None
    fill_amount: Decimal | None = None
    fill_quantity: Decimal | None = None
    first_fill_amount: Decimal | None = None
    first_fill_quantity: Decimal | None = None
    activation_profile_id: int | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    actor: str = ""


@dataclass(frozen=True)
class Fence:
    seq: int
    tranche_index: int
    proposal_id: UUID
    notional: Decimal
    kst_day: date
    approval_mode: str
    attempt_id: UUID


@dataclass(frozen=True)
class TrancheState:
    index: int
    armed: bool = False
    #: proposals of this tranche that no terminal event has closed yet
    open_proposals: tuple[UUID, ...] = ()
    completed: bool = False
    completion_event: str | None = None
    filled_amount: Decimal = ZERO
    filled_quantity: Decimal = ZERO
    #: latest ``trade_date`` carried by a fill event: the tranche's settle day (I6)
    settled_on: date | None = None
    #: fences with neither a release nor a terminal event of their proposal
    unclosed_fences: tuple[int, ...] = ()


@dataclass(frozen=True)
class PlanState:
    authority: str | None = None  # None = no events yet
    cleanup: str = "none"  # none|winding_down|orders_clear|disposable|released
    last_seq: int = 0
    current_terms_id: int | None = None
    current_terms_sha256: str | None = None
    effective_session: date | None = None
    first_fill_amount: Decimal | None = None
    first_fill_quantity: Decimal | None = None
    ever_approved: bool = False
    thesis_broken_seqs: tuple[int, ...] = ()
    holdings_disposition: str | None = None
    tranches: tuple[TrancheState, ...] = tuple(
        TrancheState(i) for i in range(1, v.MAX_TRANCHES + 1)
    )
    fences: tuple[Fence, ...] = ()
    proposals: tuple[tuple[int, UUID], ...] = ()  # (tranche_index, proposal_id)
    closed_proposals: tuple[UUID, ...] = ()
    released_fence_seqs: tuple[int, ...] = ()

    def tranche(self, index: int) -> TrancheState:
        return self.tranches[index - 1]

    @property
    def unclosed_fences(self) -> tuple[int, ...]:
        return tuple(s for t in self.tranches for s in t.unclosed_fences)

    @property
    def released(self) -> bool:
        return self.cleanup == "released"


def _refuse(code: v.MandateRefusal, message: str) -> InvalidPlanEvent:
    return InvalidPlanEvent(code, message)


def _set_tranche(state: PlanState, tranche: TrancheState) -> PlanState:
    items = list(state.tranches)
    items[tranche.index - 1] = tranche
    return replace(state, tranches=tuple(items))


def _unclosed(state: PlanState, index: int) -> tuple[int, ...]:
    return tuple(
        f.seq
        for f in state.fences
        if f.tranche_index == index
        and f.seq not in state.released_fence_seqs
        and f.proposal_id not in state.closed_proposals
    )


def _require(condition: bool, code: v.MandateRefusal, message: str) -> None:
    if not condition:
        raise _refuse(code, message)


def _check_cleanup_state(state: PlanState, event: str) -> None:
    _require(
        state.authority in v.CLEANUP_AUTH,
        v.MandateRefusal.INVALID_TRANSITION,
        f"{event} needs a plan that no longer holds sending authority",
    )


def apply_event(state: PlanState, event: PlanEvent) -> PlanState:
    """The state after ``event``; raises ``InvalidPlanEvent`` if it is not allowed.

    Mirrors the DB event trigger (``review.mandate_plan_events_guard``) so the
    writer refuses early with a closed code and the trigger refuses for any
    other writer. ``seq`` is assigned by the stream (``last_seq + 1``).
    """
    name = event.event
    if name not in v.ALL_EVENTS:
        raise _refuse(v.MandateRefusal.INVALID_TRANSITION, f"unknown event {name!r}")
    if state.authority is None:
        _require(
            name == "proposed",
            v.MandateRefusal.INVALID_TRANSITION,
            "a plan's first event is proposed",
        )
        return replace(state, authority="proposed", last_seq=state.last_seq + 1)
    _require(
        name != "proposed",
        v.MandateRefusal.INVALID_TRANSITION,
        "proposed opens a stream once",
    )
    _require(
        not state.released,
        v.MandateRefusal.INVALID_TRANSITION,
        "a released plan takes no more events",
    )
    seq = state.last_seq + 1
    state = replace(state, last_seq=seq)

    if name in v.AUTH_FROM:
        _require(
            state.authority in v.AUTH_FROM[name],
            v.MandateRefusal.INVALID_TRANSITION,
            f"{name} cannot follow authority {state.authority}",
        )
        return _apply_authority_event(state, event)

    if name == "thesis_evidence_refreshed":
        _require(
            event.thesis_seq in state.thesis_broken_seqs,
            v.MandateRefusal.INVALID_TRANSITION,
            "thesis_seq must name a thesis_broken event of this plan",
        )
        return state
    if name in v.CLEANUP_EVENTS:
        return _apply_cleanup_event(state, event)
    return _apply_tranche_event(state, event)


def _apply_authority_event(state: PlanState, event: PlanEvent) -> PlanState:
    name = event.event
    if name == "first_tranche_settled":
        t1 = state.tranche(1)
        _require(
            t1.completed and state.first_fill_amount is not None,
            v.MandateRefusal.INVALID_TRANSITION,
            "first_tranche_settled needs a completed first tranche",
        )
    if name == "exhausted":
        _require(
            state.tranche(3).completed,
            v.MandateRefusal.INVALID_TRANSITION,
            "exhausted needs a completed third tranche",
        )
    if name == "resumed":
        _require(
            event.resume_target in v.RESUME_TARGETS,
            v.MandateRefusal.INVALID_FIELDS,
            "resume_target must be approved or active",
        )
        expected = "active" if state.tranche(1).completed else "approved"
        _require(
            event.resume_target == expected,
            v.MandateRefusal.INVALID_TRANSITION,
            f"resume_target must be {expected} for this plan",
        )
        # The approval (or rebind) date is a lower bound: a resume cannot pull the
        # plan back before the session its batch approval was recorded for (§5.1).
        _require(
            state.effective_session is None
            or event.effective_session is None
            or event.effective_session >= state.effective_session,
            v.MandateRefusal.INVALID_TRANSITION,
            "a resume cannot take effect before the approval it follows",
        )
        return replace(
            state,
            authority=event.resume_target,
            effective_session=event.effective_session,
        )
    updates: dict[str, Any] = {}
    if name in ("approved", "terms_rebound"):
        updates.update(
            current_terms_id=event.terms_id,
            current_terms_sha256=event.terms_sha256,
            effective_session=event.effective_session,
        )
    if name == "approved":
        updates["ever_approved"] = True
    if name == "thesis_broken":
        updates["thesis_broken_seqs"] = (*state.thesis_broken_seqs, state.last_seq)
    new_auth = v.AUTH_AFTER.get(name, state.authority)
    cleanup = state.cleanup
    if new_auth in ("rejected", "retired", "closed") and cleanup == "none":
        cleanup = "winding_down"
    return replace(state, authority=new_auth, cleanup=cleanup, **updates)


def _apply_cleanup_event(state: PlanState, event: PlanEvent) -> PlanState:
    name = event.event
    _check_cleanup_state(state, name)
    if name == "orders_settled":
        _require(
            state.cleanup in ("none", "winding_down"),
            v.MandateRefusal.INVALID_TRANSITION,
            "orders_settled is written once",
        )
        _require(
            not state.unclosed_fences,
            v.MandateRefusal.TRANCHE_UNCERTAIN,
            "orders_settled needs every send fence closed",
        )
        _require(
            not any(t.open_proposals for t in state.tranches),
            v.MandateRefusal.TRANCHE_UNCERTAIN,
            "orders_settled needs every tranche proposal closed",
        )
        return replace(state, cleanup="orders_clear")
    if name == "holdings_disposed":
        _require(
            state.cleanup == "orders_clear",
            v.MandateRefusal.INVALID_TRANSITION,
            "holdings_disposed follows orders_settled",
        )
        kind = event.evidence.get("holdings_disposition")
        _require(
            kind in v.HOLDINGS_DISPOSITIONS,
            v.MandateRefusal.INVALID_FIELDS,
            "holdings_disposed needs evidence.holdings_disposition",
        )
        _require(
            kind != "never_adopted" or not state.ever_approved,
            v.MandateRefusal.INVALID_FIELDS,
            "never_adopted is only for a plan that was never approved",
        )
        return replace(state, cleanup="disposable", holdings_disposition=str(kind))
    # released
    _require(
        state.cleanup == "disposable",
        v.MandateRefusal.INVALID_TRANSITION,
        "released follows orders_settled and holdings_disposed",
    )
    _require(
        not state.unclosed_fences,
        v.MandateRefusal.TRANCHE_UNCERTAIN,
        "released needs every send fence closed",
    )
    _require(
        not any(t.open_proposals for t in state.tranches),
        v.MandateRefusal.TRANCHE_UNCERTAIN,
        "released needs every tranche proposal closed",
    )
    return replace(state, cleanup="released")


def _apply_tranche_event(state: PlanState, event: PlanEvent) -> PlanState:
    name = event.event
    k = event.tranche_index
    if k is None or not 1 <= k <= v.MAX_TRANCHES:
        raise _refuse(v.MandateRefusal.INVALID_FIELDS, "tranche_index must be 1..3")
    tr = state.tranche(k)
    seq = state.last_seq

    if name == "tranche_armed":
        _require_sending(state, name)
        _require(
            not tr.completed
            and not tr.armed
            and not tr.open_proposals
            and not tr.unclosed_fences,
            v.MandateRefusal.TRANCHE_DUPLICATE,
            "tranche is completed, armed, or has an open proposal or fence",
        )
        _require(
            k == 1 or state.tranche(k - 1).completed,
            v.MandateRefusal.INVALID_TRANSITION,
            "tranches run in order (I7)",
        )
        return _set_tranche(state, replace(tr, armed=True))

    if name == "tranche_proposed":
        _require_sending(state, name)
        assert event.proposal_id is not None
        _require(
            tr.armed and not tr.completed,
            v.MandateRefusal.INVALID_TRANSITION,
            "tranche_proposed needs an armed, incomplete tranche",
        )
        _require(
            not tr.open_proposals and not tr.unclosed_fences,
            v.MandateRefusal.TRANCHE_DUPLICATE,
            "the tranche already has an open proposal or fence (I9)",
        )
        _require(
            all(p != event.proposal_id for _i, p in state.proposals),
            v.MandateRefusal.TRANCHE_DUPLICATE,
            "proposal already used by this plan",
        )
        state = replace(state, proposals=(*state.proposals, (k, event.proposal_id)))
        return _set_tranche(
            state,
            replace(tr, armed=False, open_proposals=(event.proposal_id,)),
        )

    if name == "tranche_send_fenced":
        _require_sending(state, name)
        assert event.proposal_id is not None and event.notional is not None
        assert event.kst_day is not None and event.attempt_id is not None
        _require(
            event.proposal_id in tr.open_proposals,
            v.MandateRefusal.INVALID_TRANSITION,
            "the fence needs the tranche's open proposal",
        )
        _require(
            not tr.unclosed_fences,
            v.MandateRefusal.TRANCHE_UNCERTAIN,
            "the tranche has a send fence that is not closed",
        )
        _require(
            state.effective_session is not None
            and event.kst_day >= state.effective_session,
            v.MandateRefusal.PLAN_NOT_ACTIVE,
            "the fence's KST day is before the effective session",
        )
        _require(
            all(f.attempt_id != event.attempt_id for f in state.fences),
            v.MandateRefusal.TRANCHE_DUPLICATE,
            "attempt id already holds a fence",
        )
        _require(
            event.approval_mode in v.APPROVAL_MODES,
            v.MandateRefusal.INVALID_FIELDS,
            "approval_mode must be auto or human",
        )
        fence = Fence(
            seq=seq,
            tranche_index=k,
            proposal_id=event.proposal_id,
            notional=event.notional,
            kst_day=event.kst_day,
            approval_mode=str(event.approval_mode),
            attempt_id=event.attempt_id,
        )
        state = replace(state, fences=(*state.fences, fence))
        return _set_tranche(state, replace(tr, unclosed_fences=_unclosed(state, k)))

    if name == "tranche_send_released":
        target = next((f for f in state.fences if f.seq == event.fenced_seq), None)
        _require(
            target is not None and target.tranche_index == k,
            v.MandateRefusal.INVALID_TRANSITION,
            "fenced_seq must name a fence of this tranche",
        )
        _require(
            event.fenced_seq not in state.released_fence_seqs
            and target is not None
            and target.proposal_id not in state.closed_proposals,
            v.MandateRefusal.INVALID_TRANSITION,
            "the fence is already closed",
        )
        assert event.fenced_seq is not None
        state = replace(
            state,
            released_fence_seqs=(*state.released_fence_seqs, event.fenced_seq),
        )
        return _set_tranche(state, replace(tr, unclosed_fences=_unclosed(state, k)))

    if name in v.TRANCHE_TERMINAL_EVENTS:
        assert event.proposal_id is not None
        _require(
            (k, event.proposal_id) in state.proposals
            and event.proposal_id not in state.closed_proposals,
            v.MandateRefusal.INVALID_TRANSITION,
            "a terminal event needs a proposal of this tranche that is not closed",
        )
        state = replace(
            state, closed_proposals=(*state.closed_proposals, event.proposal_id)
        )
        amount = tr.filled_amount + (event.fill_amount or ZERO)
        quantity = tr.filled_quantity + (event.fill_quantity or ZERO)
        trade_date = _trade_date(event)
        settled_on = (
            max(d for d in (tr.settled_on, trade_date) if d is not None)
            if trade_date or tr.settled_on
            else None
        )
        new = replace(
            tr,
            open_proposals=tuple(
                p for p in tr.open_proposals if p != event.proposal_id
            ),
            filled_amount=amount,
            filled_quantity=quantity,
            settled_on=settled_on,
        )
        new = replace(new, unclosed_fences=_unclosed(state, k))
        if name == "tranche_filled":
            new = replace(new, completed=True, completion_event=name, armed=False)
            if k == 1:
                _require(
                    event.first_fill_amount == amount
                    and event.first_fill_quantity == quantity,
                    v.MandateRefusal.INVALID_FIELDS,
                    "first_fill_* must equal the first tranche's cumulative fills",
                )
                state = replace(
                    state,
                    first_fill_amount=event.first_fill_amount,
                    first_fill_quantity=event.first_fill_quantity,
                )
        return _set_tranche(state, new)

    # tranche_closed_partial
    assert name == "tranche_closed_partial"
    _require(
        state.authority in v.CLOSED_PARTIAL_AUTH,
        v.MandateRefusal.PLAN_NOT_ACTIVE,
        "tranche_closed_partial needs a live plan",
    )
    _require(
        not tr.completed and not tr.open_proposals and not tr.unclosed_fences,
        v.MandateRefusal.TRANCHE_DUPLICATE,
        "the tranche is completed or has an open proposal or fence",
    )
    _require(
        tr.filled_amount > 0,
        v.MandateRefusal.INVALID_TRANSITION,
        "a tranche with no fill cannot be completed",
    )
    if k == 1:
        _require(
            event.first_fill_amount == tr.filled_amount
            and event.first_fill_quantity == tr.filled_quantity,
            v.MandateRefusal.INVALID_FIELDS,
            "first_fill_* must equal the first tranche's cumulative fills",
        )
        state = replace(
            state,
            first_fill_amount=event.first_fill_amount,
            first_fill_quantity=event.first_fill_quantity,
        )
    return _set_tranche(
        state,
        replace(tr, completed=True, completion_event=name, armed=False),
    )


def _require_sending(state: PlanState, name: str) -> None:
    _require(
        state.authority in v.SEND_AUTHORITY,
        v.MandateRefusal.PLAN_NOT_ACTIVE,
        f"{name} needs authority approved or active, not {state.authority}",
    )


def _trade_date(event: PlanEvent) -> date | None:
    raw = event.evidence.get("trade_date")
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw)
        except ValueError:
            return None
    return None


def fold_plan_events(events: list[PlanEvent] | tuple[PlanEvent, ...]) -> PlanState:
    """The state of a stream, in ``seq`` order; an illegal stream raises."""
    state = PlanState()
    for expected, event in enumerate(sorted(events, key=lambda e: e.seq), start=1):
        if event.seq != expected:
            raise _refuse(
                v.MandateRefusal.INVALID_TRANSITION,
                f"seq must follow the last seq (expected {expected}, got {event.seq})",
            )
        state = apply_event(state, event)
    return state
