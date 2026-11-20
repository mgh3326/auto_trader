"""The send-fence transaction: the one boundary of mandate-bound send authority.

Design §4.3 (나) and §4.6 (hk 5286). A plan-bound rung's broker call happens only
after this module has *committed* a ``tranche_send_fenced`` event, and at most one
POST per fence. The caller's own decisions (dispatch, approval callback) are advice;
this transaction re-reads everything under the fence-area locks and its verdict and
the INSERT are one transaction on a **separate session**, so the caller losing its
session or its locks cannot change what is committed.

Steps (each read below runs after the lock before it)::

    SET TRANSACTION READ COMMITTED; SET LOCAL lock_timeout, statement_timeout
    F1  freeze scope        shared advisory (global, then realm:basket)
    F2  basket authority    FOR SHARE; a missing row refuses (nothing would be locked)
    F3s sleeve mutex        FOR UPDATE (human and automatic fence alike)
    -- an automatic fence continues with F3 (basket day) and F4 (global day), which
       belong to T5's cap check; here they are taken, in order, only when the caller
       passes T5's ``AutoFenceInputs.cap_check``
    read: plan events folded, clause/approval hashes, proposal identity, freeze verdict,
          account reference; automatic: switch, sleeve, clause, latest profile
    INSERT tranche_send_fenced; COMMIT

A refusal rolls everything back and writes nothing. If the commit cannot be
confirmed the caller does not send (``mandate_fence_unavailable``); if it did commit,
the fence stays open and blocks the tranche. An open fence is closed by exactly two
writers: ``release_send_fence`` (a proven non-send or an explicit rejection) or the
T4b projection's closing events for a ledger-proven result. A release that fails
every attempt (database down) is not retried by any job: the fence stays open, the
safe direction, and an operator acts (a durable pending-release record is a
follow-up with the send-key link of X26).

Release (``release_send_fence``) is written only for a proven-unsent attempt or an
explicit broker rejection; every ambiguous outcome keeps the fence.

The module imports no order or broker module (static test). It only ever refuses.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST
from app.models.mandate import (
    MandatePlan,
    MandatePlanEvent,
    MandatePlanTranche,
    MandateTerms,
)
from app.models.order_proposals import OrderProposal
from app.models.strategy_sleeves import StrategySleeve, StrategySleeveVersion
from app.services.mandate import fence_locks, proposal_gate, store
from app.services.mandate import vocabulary as v
from app.services.mandate.buy_freeze import buy_freeze_state
from app.services.mandate.fence_rules import (
    AutoFacts,
    SendFacts,
    auto_refusal,
    is_blocking,
    send_refusal,
)
from app.services.mandate.fold import (
    InvalidPlanEvent,
    PlanEvent,
    PlanState,
    apply_event,
)
from app.services.mandate.invariants import account_binding_ok
from app.services.strategy_sleeves.vocabulary import SleeveState

logger = logging.getLogger(__name__)

R = v.MandateRefusal

#: Constants, not policy values (§4.6): a fence that cannot lock or finish quickly
#: sends nothing.
FENCE_LOCK_TIMEOUT_MS = 2_000
FENCE_STATEMENT_TIMEOUT_MS = 8_000
#: One re-read after losing a ``seq`` race (§4.6 "seq가 겹치면 ... 한 번 더").
MAX_ATTEMPTS = 2

SessionFactory = Callable[[], AsyncSession]


def default_session_factory() -> AsyncSession:
    from app.core.db import AsyncSessionLocal

    return AsyncSessionLocal()


@dataclass(frozen=True)
class AutoFenceInputs:
    """What only an automatic fence needs. The automatic path is opened by T5.

    ``cap_check`` is T5's B/G cap judgement; it runs after F3 and F4 are held and
    returns a refusal code or ``None``. Without it, no automatic fence is written.
    """

    switch_enabled: bool
    judged_terms_id: int | None = None
    cap_check: Callable[[AsyncSession, Any], Awaitable[str | None]] | None = None


@dataclass(frozen=True)
class FenceRequest:
    plan_id: int
    tranche_index: int
    proposal_id: UUID
    approval_mode: str  # 'human' | 'auto'
    #: max(limit, current) x quantity, the figure the person or the classifier saw
    notional: Decimal
    #: the reference computed from the account the request will really use (§4.6
    #: "계좌 결속"); the caller's ``broker_account_id`` is never an input
    actual_account_ref: str | None
    now: datetime
    attempt_id: UUID = field(default_factory=uuid4)
    auto: AutoFenceInputs | None = None
    #: the decision clock, sampled by the fence **after** its locks (I11: a measurement
    #: that expired while the fence waited must not authorize it); ``None`` = real time
    clock: Callable[[], datetime] | None = field(
        default=None, compare=False, repr=False
    )


@dataclass(frozen=True)
class FenceOutcome:
    """``fenced`` is true only for a committed fence; otherwise ``refusal`` is a closed code."""

    fenced: bool
    seq: int | None = None
    attempt_id: UUID | None = None
    refusal: str | None = None
    detail: dict[str, str] = field(default_factory=dict)

    @property
    def blocking(self) -> bool:
        return self.refusal is not None and is_blocking(self.refusal)


def _refused(code: str, **detail: str) -> FenceOutcome:
    return FenceOutcome(False, refusal=code, detail=dict(detail))


class _SeqRace(Exception):
    """Another event writer took the ``seq``; re-read and judge once more."""


class _Refuse(Exception):
    def __init__(self, code: str, **detail: str) -> None:
        super().__init__(code)
        self.code = code
        self.detail = detail


async def _begin(session: AsyncSession) -> None:
    await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
    await session.execute(text(f"SET LOCAL lock_timeout = '{FENCE_LOCK_TIMEOUT_MS}ms'"))
    await session.execute(
        text(f"SET LOCAL statement_timeout = '{FENCE_STATEMENT_TIMEOUT_MS}ms'")
    )


def _frozen(verdict: Any, now: datetime) -> bool:
    return (
        verdict.frozen
        or verdict.valid_until is None
        or verdict.valid_until.utcoffset() is None
        or verdict.valid_until <= now
    )


def _real_now() -> datetime:
    return datetime.now(UTC)


async def _read_freeze(
    session: AsyncSession,
    sleeve: StrategySleeve,
    clock: Callable[[], datetime],
    freeze_fn: Callable[..., Awaitable[Any]],
) -> bool | None:
    """The verdict is judged on a clock sampled again **after** the reader returns, so a
    measurement that expires while the reader runs cannot authorize a fence (I11)."""
    try:
        verdict = await freeze_fn(
            session,
            account_mode=sleeve.account_mode,
            market=sleeve.market,
            sleeve_id=sleeve.id,
            now=clock(),
        )
        return _frozen(verdict, clock())
    except Exception:  # noqa: BLE001 - a freeze that cannot be read is a freeze
        return True


@dataclass(frozen=True)
class _Loaded:
    plan: MandatePlan
    sleeve: StrategySleeve
    original_terms: MandateTerms
    realm: str
    basket_key: str


async def _load_identity(session: AsyncSession, plan_id: int) -> _Loaded:
    plan = await store.get_plan(session, plan_id)
    if plan is None:
        raise _Refuse(R.BINDING_MISMATCH.value, why="plan_missing")
    sleeve = await session.get(StrategySleeve, plan.sleeve_id)
    original = await session.get(MandateTerms, plan.approved_terms_id)
    if sleeve is None or original is None:
        raise _Refuse(R.BINDING_BROKEN.value, why="plan_identity_missing")
    realm, _market = store.realm_and_market(sleeve)
    return _Loaded(plan, sleeve, original, realm, original.basket_key)


async def _take_fence_locks(session: AsyncSession, loaded: _Loaded) -> None:
    """F1 shared, F2 shared, F3s — the human fence's whole set, in order."""
    try:
        await fence_locks.acquire_freeze_shared(
            session, loaded.realm, loaded.basket_key
        )
        await fence_locks.lock_authority_shared(
            session, loaded.realm, loaded.basket_key
        )
    except fence_locks.AuthorityRowMissing as exc:
        raise _Refuse(R.UNREADABLE.value, why="authority_row_missing") from exc
    await fence_locks.lock_mutex(
        session, fence_locks.sleeve_mutex_key(loaded.sleeve.id), "sleeve"
    )


async def _terms_integrity(
    session: AsyncSession, state: PlanState, loaded: _Loaded
) -> tuple[MandateTerms | None, bool]:
    terms = (
        await session.get(MandateTerms, state.current_terms_id)
        if state.current_terms_id is not None
        else loaded.original_terms
    )
    if terms is None or terms.basket_key != loaded.basket_key:
        return None, False
    version = await session.get(StrategySleeveVersion, terms.sleeve_version_id)
    if version is None:
        return None, False
    ok = (
        state.current_terms_sha256 == terms.content_sha256
        and await proposal_gate.approval_matches(
            session,
            terms.approval_id,
            terms.content_sha256,
            store.terms_content_from_rows(terms, version),
            loaded.plan.sleeve_id,
            "terms",
        )
    )
    return terms, ok


async def _proposal_identity_ok(
    session: AsyncSession, req: FenceRequest, loaded: _Loaded
) -> bool:
    group = (
        await session.execute(
            select(OrderProposal).where(OrderProposal.proposal_id == req.proposal_id)
        )
    ).scalar_one_or_none()
    return (
        group is not None
        and group.mandate_plan_id == loaded.plan.id
        and group.mandate_tranche_index == req.tranche_index
        and group.symbol == loaded.plan.symbol
        and group.market == loaded.sleeve.market
        and group.account_mode == loaded.sleeve.account_mode
        and group.sleeve_id == loaded.plan.sleeve_id
    )


async def gather_send_facts(
    session: AsyncSession,
    req: FenceRequest,
    loaded: _Loaded,
    *,
    freeze_fn: Callable[..., Awaitable[Any]] = buy_freeze_state,
) -> tuple[SendFacts, MandateTerms | None, PlanState]:
    state = await store.plan_state(session, loaded.plan.id)
    tranche_row = await session.get(
        MandatePlanTranche, (loaded.plan.id, req.tranche_index)
    )
    terms, terms_ok = await _terms_integrity(session, state, loaded)
    identity = await _proposal_identity_ok(session, req, loaded)
    bound_ok = terms is not None and account_binding_ok(
        terms_account_ref=terms.account_ref,
        plan_account_ref=loaded.plan.account_ref,
        scope_account_refs=loaded.plan.scope_account_refs,
        realm=loaded.realm,
    )
    facts = SendFacts(
        state=state,
        tranche_index=req.tranche_index,
        proposal_id=req.proposal_id,
        kst_day=req.now.astimezone(KST).date(),
        notional=req.notional,
        planned_amount=Decimal(str(loaded.plan.planned_amount)),
        adopted_amount=Decimal(str(loaded.plan.adopted_amount)),
        integrity_ok=tranche_row is not None and terms is not None and terms_ok,
        identity_ok=identity,
        account_binding_ok=bound_ok,
        actual_account_ref=req.actual_account_ref,
        plan_account_ref=loaded.plan.account_ref,
        frozen=await _read_freeze(
            session, loaded.sleeve, req.clock or _real_now, freeze_fn
        ),
    )
    return facts, terms, state


async def gather_auto_facts(
    session: AsyncSession,
    req: FenceRequest,
    loaded: _Loaded,
    state: PlanState,
) -> tuple[AutoFacts, int | None]:
    assert req.auto is not None
    version, live_terms = await store.current_terms(session, loaded.plan.sleeve_id)
    sleeve_live = version is not None and version.state == SleeveState.LIVE.value
    profile = await store.latest_profile(session, loaded.plan.sleeve_id)
    profile_ok = False
    if profile is not None and live_terms is not None and version is not None:
        profile_ok = await proposal_gate.approval_matches(
            session,
            profile.approval_id,
            profile.content_sha256,
            store.profile_content_from_row(profile),
            loaded.plan.sleeve_id,
            "activation_profile",
        )
    kst_day = req.now.astimezone(KST).date()
    facts = AutoFacts(
        switch_enabled=req.auto.switch_enabled,
        sleeve_live=sleeve_live,
        live_terms_id=None if live_terms is None else live_terms.id,
        live_terms_expired=live_terms is not None and live_terms.expires_at <= req.now,
        plan_terms_id=state.current_terms_id,
        judged_terms_id=req.auto.judged_terms_id,
        profile_id=None if profile is None else profile.id,
        profile_terms_id=None if profile is None else profile.terms_id,
        profile_max_tranche_index=0 if profile is None else profile.max_tranche_index,
        profile_effective_ok=profile is not None
        and profile.effective_session <= kst_day,
        profile_expired=profile is not None and profile.expires_at <= req.now,
        profile_approval_ok=profile_ok,
        tranche_index=req.tranche_index,
    )
    return facts, None if profile is None else profile.id


def _map_insert_error(exc: DBAPIError) -> _Refuse | _SeqRace:
    text_ = str(getattr(exc, "orig", exc))
    if "must follow the last seq" in text_ or "uq_mandate_plan_events" in text_:
        return _SeqRace()
    if "duplicate key" in text_ and "plan_events" in text_:
        return _SeqRace()
    if "sending authority" in text_ or "effective session" in text_:
        return _Refuse(R.PLAN_NOT_ACTIVE.value)
    if "not closed" in text_:
        return _Refuse(R.TRANCHE_UNCERTAIN.value)
    if "open proposal" in text_:
        return _Refuse(R.TRANCHE_DUPLICATE.value)
    if "profile" in text_ or "clause" in text_:
        return _Refuse(R.ACTIVATION_MISSING.value)
    if "plan account" in text_:
        return _Refuse(R.ACCOUNT_MISMATCH.value)
    return _Refuse(R.FENCE_UNAVAILABLE.value)


async def _fence_once(
    session: AsyncSession,
    req: FenceRequest,
    freeze_fn: Callable[..., Awaitable[Any]],
) -> FenceOutcome:
    await _begin(session)
    loaded = await _load_identity(session, req.plan_id)
    await _take_fence_locks(session, loaded)
    auto = req.approval_mode == "auto"
    if auto:
        if req.auto is None:
            raise _Refuse(R.SWITCH_OFF.value, why="no_auto_inputs")
        if req.auto.cap_check is not None:
            await fence_locks.lock_mutex(
                session,
                fence_locks.basket_day_mutex_key(
                    loaded.realm, loaded.basket_key, req.now.astimezone(KST).date()
                ),
                "basket",
            )
            await fence_locks.lock_mutex(
                session,
                fence_locks.global_day_mutex_key(
                    loaded.sleeve.account_mode,
                    loaded.sleeve.market,
                    req.now.astimezone(KST).date(),
                ),
                "global",
            )
    facts, _terms, state = await gather_send_facts(
        session, req, loaded, freeze_fn=freeze_fn
    )
    refusal = send_refusal(facts)
    if refusal is not None:
        raise _Refuse(refusal)
    profile_id: int | None = None
    if auto:
        assert req.auto is not None
        if req.auto.cap_check is None:
            raise _Refuse(R.SWITCH_OFF.value, why="caps_not_wired")
        auto_facts, profile_id = await gather_auto_facts(session, req, loaded, state)
        refusal = auto_refusal(auto_facts)
        if refusal is not None:
            raise _Refuse(refusal)
        cap_refusal = await req.auto.cap_check(session, facts)
        if cap_refusal is not None:
            raise _Refuse(cap_refusal)
    seq = state.last_seq + 1
    fields: dict[str, Any] = {
        "tranche_index": req.tranche_index,
        "proposal_id": req.proposal_id,
        "notional": req.notional,
        "kst_day": facts.kst_day,
        "approval_mode": req.approval_mode,
        "account_ref": loaded.plan.account_ref,
        "attempt_id": req.attempt_id,
        "activation_profile_id": profile_id,
    }
    problem = store._event_field_problem("tranche_send_fenced", fields)
    if problem is not None:
        raise _Refuse(R.FENCE_UNAVAILABLE.value, why="fields")
    try:
        apply_event(
            state,
            PlanEvent(
                event="tranche_send_fenced",
                seq=seq,
                actor=f"system:mandate_fence:{req.approval_mode}",
                **fields,
            ),
        )
    except InvalidPlanEvent as exc:
        raise _Refuse(exc.code.value) from exc
    row = MandatePlanEvent(
        plan_id=loaded.plan.id,
        seq=seq,
        event="tranche_send_fenced",
        evidence={},
        actor=f"system:mandate_fence:{req.approval_mode}",
        **fields,
    )
    session.add(row)
    try:
        await session.flush()
    except DBAPIError as exc:
        mapped = _map_insert_error(exc)
        raise mapped from exc
    return FenceOutcome(True, seq=seq, attempt_id=req.attempt_id)


async def commit_send_fence(
    req: FenceRequest,
    *,
    session_factory: SessionFactory | None = None,
    freeze_fn: Callable[..., Awaitable[Any]] = buy_freeze_state,
) -> FenceOutcome:
    """Judge and commit one fence on its own session. Never raises, never sends.

    ``fenced`` true is the only thing that lets the caller start the broker call, and
    only once. Every other outcome — a refusal, a timeout, an unreadable row, an
    unconfirmed commit — means no broker call.
    """
    if req.approval_mode not in ("auto", "human"):
        return _refused(R.INVALID_FIELDS.value, why="approval_mode")
    factory = session_factory or default_session_factory
    for _attempt in range(MAX_ATTEMPTS):
        session = factory()
        try:
            try:
                outcome = await _fence_once(session, req, freeze_fn)
            except _SeqRace:
                await session.rollback()
                continue
            except _Refuse as refuse:
                await session.rollback()
                return _refused(refuse.code, **refuse.detail)
            except Exception:  # noqa: BLE001 - the fence is not written
                logger.warning("mandate fence transaction failed", exc_info=True)
                await _quiet_rollback(session)
                return _refused(R.FENCE_UNAVAILABLE.value, why="transaction_error")
            try:
                await session.commit()
            except Exception:  # noqa: BLE001 - an unconfirmed commit sends nothing
                logger.warning("mandate fence commit not confirmed", exc_info=True)
                return _refused(R.FENCE_UNAVAILABLE.value, why="commit_unconfirmed")
            return outcome
        finally:
            await _quiet_close(session)
    return _refused(R.FENCE_UNAVAILABLE.value, why="seq_race")


async def _quiet_rollback(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:  # noqa: BLE001
        logger.debug("rollback after a failed fence", exc_info=True)


async def _quiet_close(session: AsyncSession) -> None:
    try:
        await session.close()
    except Exception:  # noqa: BLE001
        logger.debug("close after a fence", exc_info=True)


# ------------------------------------------------------------------- release
@dataclass(frozen=True)
class ReleaseOutcome:
    released: bool
    reason: str


async def _release_once(
    plan_id: int,
    fenced_seq: int,
    *,
    reason_code: str,
    session_factory: SessionFactory | None = None,
) -> ReleaseOutcome:
    """Close a fence as *proven unsent* or *explicitly rejected* — nothing else.

    Lock order F3s, then (automatic fence) F3 and F4, the order a fence takes them.
    A fence that is already closed, or a failure to write, leaves things as they are:
    an unclosed fence blocks the tranche and holds its amount, the safe direction.
    """
    if reason_code not in v.RELEASE_REASONS:
        return ReleaseOutcome(False, "reason_not_allowed")
    factory = session_factory or default_session_factory
    session = factory()
    try:
        await _begin(session)
        loaded = await _load_identity(session, plan_id)
        events = await store.list_plan_events(session, plan_id)
        fence = next(
            (
                e
                for e in events
                if e.event == "tranche_send_fenced" and e.seq == fenced_seq
            ),
            None,
        )
        if fence is None:
            await session.rollback()
            return ReleaseOutcome(False, "no_such_fence")
        await fence_locks.lock_mutex(
            session, fence_locks.sleeve_mutex_key(loaded.sleeve.id), "sleeve"
        )
        if fence.approval_mode == "auto":
            await fence_locks.lock_mutex(
                session,
                fence_locks.basket_day_mutex_key(
                    loaded.realm, loaded.basket_key, fence.kst_day
                ),
                "basket",
            )
            await fence_locks.lock_mutex(
                session,
                fence_locks.global_day_mutex_key(
                    loaded.sleeve.account_mode, loaded.sleeve.market, fence.kst_day
                ),
                "global",
            )
        state = await store.plan_state(session, plan_id)
        if fenced_seq not in state.unclosed_fences:
            await session.rollback()
            return ReleaseOutcome(False, "already_closed")
        session.add(
            MandatePlanEvent(
                plan_id=plan_id,
                seq=state.last_seq + 1,
                event="tranche_send_released",
                tranche_index=fence.tranche_index,
                fenced_seq=fenced_seq,
                evidence={"reason_code": reason_code},
                actor="system:mandate_fence",
            )
        )
        await session.flush()
        await session.commit()
        return ReleaseOutcome(True, reason_code)
    except Exception:  # noqa: BLE001 - the fence stays; a retry of the release is safe
        logger.warning("mandate fence release failed", exc_info=True)
        await _quiet_rollback(session)
        return ReleaseOutcome(False, "release_failed")
    finally:
        await _quiet_close(session)


#: A release that loses a ``seq`` race to another event writer is re-read and tried
#: again: a fence known never to have sent must not stay open for want of one retry.
MAX_RELEASE_ATTEMPTS = 3


async def release_send_fence(
    plan_id: int,
    fenced_seq: int,
    *,
    reason_code: str,
    session_factory: SessionFactory | None = None,
) -> ReleaseOutcome:
    """Close a fence as *proven unsent* or *explicitly rejected* — nothing else.

    See ``_release_once``; ``released`` is true only for a committed release, and
    ``already_closed`` means someone else closed it. ``release_failed`` after every
    attempt leaves the fence open (the safe direction) and the caller logs it.
    """
    outcome = ReleaseOutcome(False, "release_failed")
    for _attempt in range(MAX_RELEASE_ATTEMPTS):
        outcome = await _release_once(
            plan_id,
            fenced_seq,
            reason_code=reason_code,
            session_factory=session_factory,
        )
        if outcome.reason != "release_failed":
            return outcome
    return outcome


# --------------------------------------------------------- best-effort rechecks
async def send_boundary_refusal(
    req: FenceRequest,
    *,
    fenced_seq: int | None = None,
    session_factory: SessionFactory | None = None,
    freeze_fn: Callable[..., Awaitable[Any]] = buy_freeze_state,
) -> str | None:
    """The same decision without locks and without writing — a fast early refusal.

    Used before the nonce is consumed (``fenced_seq=None``) and just before the POST
    (``fenced_seq`` = the fence this send holds, which must still be open). A fact
    that cannot be read refuses (``mandate_unreadable``). This is advice: it can be
    overtaken by a commit right after it, and the fence transaction decides alone.
    """
    factory = session_factory or default_session_factory
    session = factory()
    try:
        await session.execute(text("SET TRANSACTION ISOLATION LEVEL READ COMMITTED"))
        await session.execute(
            text(f"SET LOCAL statement_timeout = '{FENCE_STATEMENT_TIMEOUT_MS}ms'")
        )
        loaded = await _load_identity(session, req.plan_id)
        facts, _terms, state = await gather_send_facts(
            session, req, loaded, freeze_fn=freeze_fn
        )
        if fenced_seq is not None:
            if fenced_seq not in state.unclosed_fences:
                return R.TRANCHE_UNCERTAIN.value
            # this send's own fence is the one open fence, so it must not block itself
            facts = _without_own_fence(facts, fenced_seq)
        return send_refusal(facts)
    except _Refuse as refuse:
        return refuse.code
    except Exception:  # noqa: BLE001 - unreadable evidence refuses
        logger.warning("mandate send recheck failed", exc_info=True)
        return R.UNREADABLE.value
    finally:
        await _quiet_rollback(session)
        await _quiet_close(session)


def _without_own_fence(facts: SendFacts, fenced_seq: int) -> SendFacts:
    from dataclasses import replace

    state = facts.state
    tranches = tuple(
        replace(
            t, unclosed_fences=tuple(s for s in t.unclosed_fences if s != fenced_seq)
        )
        for t in state.tranches
    )
    return replace(facts, state=replace(state, tranches=tranches))
