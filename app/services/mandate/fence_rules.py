"""Pure send-fence decisions (hk 3795 T4c, §4.3 (나), §4.6, §4.7).

No database, no clock, no network: every input is a value the fence transaction has
just read under its locks (or, for the best-effort checks, a fresh short read). The
same function answers in the fence transaction, in the approval callback before the
nonce is consumed, and just before the POST, so the three places cannot disagree.

A refusal is a closed ``mandate_*`` code. ``BLOCKING`` codes mean the send does not
happen even if a person approves it (§4.7 "차단"); ``DEMOTING`` codes are only about
automatic approval, so a person may still approve the same proposal (§4.7 "강등").
Everything here only ever refuses: nothing in this module grants a send.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from app.services.mandate import vocabulary as v
from app.services.mandate.fold import PlanState

R = v.MandateRefusal

#: The sleeve gate's reason for a version that is not live (ROB-1470 code, unchanged).
SLEEVE_NOT_LIVE = "sleeve_not_live"

#: Refusals after which a person's approval does not send either (§4.7).
BLOCKING: frozenset[str] = frozenset(
    {
        R.BINDING_BROKEN.value,
        R.BINDING_MISMATCH.value,
        R.ACCOUNT_MISMATCH.value,
        R.PLAN_NOT_ACTIVE.value,
        R.TRANCHE_DUPLICATE.value,
        R.TRANCHE_UNCERTAIN.value,
        R.PLAN_AMOUNT_EXCEEDED.value,
        R.DRAWDOWN_FROZEN.value,
        R.UNREADABLE.value,
        R.FENCE_UNAVAILABLE.value,
        R.THESIS_NOT_BROKEN.value,
    }
)

#: Refusals that only stop an automatic fence; the proposal goes to a person (§4.7).
DEMOTING: frozenset[str] = frozenset(
    {
        R.SWITCH_OFF.value,
        R.ACTIVATION_MISSING.value,
        R.TRANCHE_NOT_ACTIVATED.value,
        R.TERMS_MISSING.value,
        R.TERMS_EXPIRED.value,
        R.TERMS_NOT_CURRENT.value,
        SLEEVE_NOT_LIVE,
    }
)


@dataclass(frozen=True)
class SendFacts:
    """What a send decision reads. ``None`` for a fact means it could not be read."""

    state: PlanState
    tranche_index: int
    proposal_id: UUID
    kst_day: date
    notional: Decimal
    planned_amount: Decimal
    adopted_amount: Decimal
    #: plan, tranche, clause rows exist and the clause/approval hashes agree (I21)
    integrity_ok: bool
    #: the proposal's plan, tranche, symbol, market, account mode and sleeve are the plan's (I15)
    identity_ok: bool
    #: plan account == clause account and the plan scope is the realm's holdings accounts
    account_binding_ok: bool
    #: the reference computed from the account the request will actually use
    actual_account_ref: str | None
    plan_account_ref: str
    #: the verdict of ``buy_freeze_state`` read under F1, already reduced to a bool
    frozen: bool | None


def send_refusal(f: SendFacts) -> str | None:
    """The first reason this send must not happen, in the order of §4.7; else ``None``."""
    if not f.integrity_ok:
        return R.BINDING_BROKEN.value
    if not f.identity_ok:
        return R.BINDING_MISMATCH.value
    state = f.state
    if (
        state.authority not in v.SEND_AUTHORITY
        or state.effective_session is None
        or f.kst_day < state.effective_session
    ):
        return R.PLAN_NOT_ACTIVE.value
    if (
        not f.account_binding_ok
        or f.actual_account_ref is None
        or f.actual_account_ref != f.plan_account_ref
    ):
        return R.ACCOUNT_MISMATCH.value
    tranche = state.tranche(f.tranche_index)
    if tranche.unclosed_fences:
        return R.TRANCHE_UNCERTAIN.value
    if tranche.completed or tranche.open_proposals != (f.proposal_id,):
        return R.TRANCHE_DUPLICATE.value
    filled = sum((t.filled_amount for t in state.tranches), Decimal(0))
    if f.notional + filled > f.planned_amount - f.adopted_amount:
        return R.PLAN_AMOUNT_EXCEEDED.value
    if f.frozen is None:
        return R.UNREADABLE.value
    if f.frozen:
        return R.DRAWDOWN_FROZEN.value
    return None


@dataclass(frozen=True)
class AutoFacts:
    """What an automatic fence additionally re-reads after F2 (§4.6 "자격의 재확인")."""

    switch_enabled: bool
    sleeve_live: bool
    #: the sleeve's current live clause id, ``None`` when the live version has none
    live_terms_id: int | None
    live_terms_expired: bool
    #: the clause the plan is currently bound to (last approved / terms_rebound event)
    plan_terms_id: int | None
    #: the clause the caller's earlier, advisory judgement used
    judged_terms_id: int | None
    #: the sleeve's latest profile (revocation is a newer version) and what it allows
    profile_id: int | None
    profile_terms_id: int | None
    profile_max_tranche_index: int
    profile_effective_ok: bool
    profile_expired: bool
    profile_approval_ok: bool
    tranche_index: int


def auto_refusal(a: AutoFacts) -> str | None:
    """Why this fence may not be automatic; ``None`` = it still may.

    Every code is a demotion (the proposal goes to a person) except an unreadable or
    broken approval record, which is a block: nothing may send on an unverifiable
    clause or profile.
    """
    if not a.switch_enabled:
        return R.SWITCH_OFF.value
    if not a.sleeve_live:
        return SLEEVE_NOT_LIVE
    if a.live_terms_id is None:
        return R.TERMS_MISSING.value
    if a.live_terms_expired:
        return R.TERMS_EXPIRED.value
    if a.plan_terms_id != a.live_terms_id or (
        a.judged_terms_id is not None and a.judged_terms_id != a.live_terms_id
    ):
        return R.TERMS_NOT_CURRENT.value
    if (
        a.profile_id is None
        or a.profile_terms_id != a.live_terms_id
        or a.profile_expired
        or not a.profile_effective_ok
        or a.profile_max_tranche_index == 0
    ):
        return R.ACTIVATION_MISSING.value
    if not a.profile_approval_ok:
        return R.BINDING_BROKEN.value
    if a.tranche_index > a.profile_max_tranche_index:
        return R.TRANCHE_NOT_ACTIVATED.value
    return None


def is_blocking(code: str) -> bool:
    """A refusal that no human approval may override. Unknown codes block (fail closed)."""
    return code not in DEMOTING


def plan_fence_exposure(state: PlanState) -> Decimal:
    """What this plan's attempts still expose to the sleeve budget (design §4.6).

    An unclosed fence counts its whole notional. A fence closed by a tranche result
    keeps counting the filled amount until the sleeve book has attributed it (the
    book overlay itself is T5's; this is the plan's side of that contract, so a
    callback rollback that loses the rung's ``broker_order_id`` cannot make a fill
    disappear). A released fence counts nothing: it was proven unsent.
    """
    total = Decimal(0)
    open_seqs = set(state.unclosed_fences)
    for fence in state.fences:
        if fence.seq in open_seqs:
            total += fence.notional
    return total + sum((t.filled_amount for t in state.tranches), Decimal(0))
