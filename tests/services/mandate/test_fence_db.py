"""hk 3795 T4c: the send-fence transaction against the CI database (§4.6, hk 5286).

Runs in CI on the run-owned test database only; the racing-session cases are not run
locally (hk host safety). Two sessions are opened and the commit order is fixed with
events; a waiting session is *observed* in ``pg_stat_activity`` before the other one
commits, so no test relies on timing alone. Test names carry the §4.6 case (X5 ...)
or the invariant (I15, I16, I24 ...) they pin.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal, engine
from app.services.mandate import fence, fence_locks
from app.services.mandate import store as ms
from app.services.mandate import vocabulary as v
from app.services.mandate.buy_freeze import FreezeVerdict
from app.services.mandate.fence_rules import plan_fence_exposure
from app.services.strategy_sleeves import store as sleeve_store
from tests._run_owned_database import validate_run_owned_database_url
from tests.services.mandate.db_support import (
    EFFECTIVE,
    PRINCIPAL,
    MandateSeed,
    digest,
    new_symbol,
)

validate_run_owned_database_url(engine.url)

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

#: 2026-11-23 10:00 KST: the plans' effective session
NOW = datetime(2026, 11, 23, 1, 0, tzinfo=UTC)
R = v.MandateRefusal


@pytest_asyncio.fixture
async def seed(db_session):
    helper = MandateSeed(db_session)
    try:
        yield helper
    finally:
        await helper.cleanup()


class Freeze:
    """The shared freeze reader a fence calls under F1; tests flip it."""

    def __init__(self) -> None:
        self.frozen = False

    async def __call__(self, session, **_kw) -> FreezeVerdict:
        return FreezeVerdict(
            frozen=self.frozen,
            reason="fixture",
            evidence={},
            valid_until=datetime.now(UTC) + timedelta(hours=1),
        )


async def _profile(db, sleeve, terms, max_index: int):
    request = await ms.create_profile_request(
        db,
        sleeve_id=sleeve.id,
        terms_id=terms.id,
        max_tranche_index=max_index,
        effective_session=EFFECTIVE,
        expires_at=datetime.now(UTC) + timedelta(days=30),
        approval_ref="hk:t4c",
        requested_by="operator:test",
    )
    await db.commit()
    return request


async def _approve_profile(db, request):
    result = await ms.record_approval(
        db,
        request_id=request.id,
        decision="approved",
        principal=PRINCIPAL,
        nonce_digest=digest("p"),
    )
    return result.profile


async def _ready(seed, db, *, profile_max: int | None = 1, **kw):
    """An approved plan with an open tranche-1 proposal and (optionally) a live profile."""
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,), **kw)
    plan = seed.plan_snapshot((await seed.register(sleeve, terms, sym)).plan)
    await seed.approve_plan(plan.id, terms)
    profile = None
    if profile_max is not None:
        profile = await _approve_profile(
            db, await _profile(db, sleeve, terms, profile_max)
        )
        await db.commit()
    proposal = await seed.bound_proposal(plan=plan, sleeve=sleeve)
    pid = proposal.proposal_id
    await ms.append_plan_event(
        db, plan_id=plan.id, event="tranche_armed", actor="o", tranche_index=1
    )
    await ms.append_plan_event(
        db,
        plan_id=plan.id,
        event="tranche_proposed",
        actor="o",
        tranche_index=1,
        proposal_id=pid,
    )
    await db.commit()
    return SimpleNamespace(
        sleeve=sleeve,
        terms=terms,
        plan=plan,
        pid=pid,
        profile=profile,
        freeze=Freeze(),
    )


async def _no_caps(session, facts):
    return None


def _req(r, *, mode="human", account=None, notional="300000", auto=None, pid=None):
    return fence.FenceRequest(
        plan_id=r.plan.id,
        tranche_index=1,
        proposal_id=pid or r.pid,
        approval_mode=mode,
        notional=Decimal(notional),
        actual_account_ref=r.plan.account_ref if account is None else account,
        now=NOW,
        auto=auto,
    )


def _auto(r, *, caps=_no_caps):
    return fence.AutoFenceInputs(
        switch_enabled=True, judged_terms_id=r.terms.id, cap_check=caps
    )


async def _commit(r, **kw):
    return await fence.commit_send_fence(_req(r, **kw), freeze_fn=r.freeze)


async def _events(db, plan_id, name: str | None = None) -> list:
    await db.rollback()
    rows = await ms.list_plan_events(db, plan_id)
    return [e for e in rows if name is None or e.event == name]


async def _blocked_sessions(minimum: int = 1, timeout: float = 10.0) -> bool:
    """True once ``minimum`` sessions of this database wait on a lock."""
    deadline = time.monotonic() + timeout
    async with AsyncSessionLocal() as probe:
        while time.monotonic() < deadline:
            count = (
                await probe.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_stat_activity "
                        "WHERE datname = current_database() "
                        "AND wait_event_type = 'Lock' AND pid <> pg_backend_pid()"
                    )
                )
            ).scalar_one()
            if count >= minimum:
                return True
            await asyncio.sleep(0.05)
    return False


# ------------------------------------------------------------------ the happy path
async def test_i24_a_human_fence_commits_and_the_next_one_waits_for_a_reconcile(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    first = await _commit(r)
    assert first.fenced and first.seq is not None
    fences = await _events(db_session, r.plan.id, "tranche_send_fenced")
    assert [f.approval_mode for f in fences] == ["human"]
    assert fences[0].account_ref == r.plan.account_ref
    assert fences[0].activation_profile_id is None

    second = await _commit(r)
    assert second.fenced is False
    assert second.refusal == R.TRANCHE_UNCERTAIN.value
    assert len(await _events(db_session, r.plan.id, "tranche_send_fenced")) == 1


async def test_i24_a_release_closes_the_fence_and_only_a_proven_unsent_reason_may(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    first = await _commit(r)
    refused = await fence.release_send_fence(
        r.plan.id, first.seq, reason_code="timeout"
    )
    assert refused.released is False
    assert (await ms.plan_state(db_session, r.plan.id)).unclosed_fences == (first.seq,)

    done = await fence.release_send_fence(
        r.plan.id, first.seq, reason_code="pre_send_refused"
    )
    assert done.released is True
    again = await fence.release_send_fence(
        r.plan.id, first.seq, reason_code="pre_send_refused"
    )
    assert (again.released, again.reason) == (False, "already_closed")
    # a new attempt may now fence again
    assert (await _commit(r)).fenced is True


async def test_i15_a_proposal_that_is_not_the_plans_is_blocked(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    stranger = await seed.loss_cut_proposal(symbol=new_symbol())
    out = await _commit(r, pid=stranger.proposal_id)
    assert (out.fenced, out.refusal) == (False, R.BINDING_MISMATCH.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_i15_the_account_the_request_will_use_must_be_the_plans(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    for account in ("acct-someone-else", None):
        out = await fence.commit_send_fence(
            fence.FenceRequest(
                plan_id=r.plan.id,
                tranche_index=1,
                proposal_id=r.pid,
                approval_mode="human",
                notional=Decimal("300000"),
                actual_account_ref=account,
                now=NOW,
            ),
            freeze_fn=r.freeze,
        )
        assert (out.fenced, out.refusal) == (False, R.ACCOUNT_MISMATCH.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_i8_a_send_beyond_the_whole_plan_amount_is_blocked(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    out = await _commit(r, notional="2350001")
    assert (out.fenced, out.refusal) == (False, R.PLAN_AMOUNT_EXCEEDED.value)


async def test_i11_the_stub_freeze_blocks_every_fence(db_session, seed) -> None:
    """Until hk 4900 supplies measurements the default reader freezes everything."""
    r = await _ready(seed, db_session)
    out = await fence.commit_send_fence(_req(r))  # the real default freeze reader
    assert (out.fenced, out.refusal) == (False, R.DRAWDOWN_FROZEN.value)


# --------------------------------------------------- order against plan transitions
async def test_i16_retired_before_the_fence_means_no_fence_and_retired_after_means_one(
    db_session, seed
) -> None:
    before = await _ready(seed, db_session)
    await ms.append_plan_event(
        db_session, plan_id=before.plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    out = await _commit(before)
    assert (out.fenced, out.refusal) == (False, R.PLAN_NOT_ACTIVE.value)
    assert await _events(db_session, before.plan.id, "tranche_send_fenced") == []

    after = await _ready(seed, db_session)
    one = await _commit(after)
    assert one.fenced is True
    await ms.append_plan_event(
        db_session, plan_id=after.plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    assert (await _commit(after)).fenced is False  # never a second fence
    assert len(await _events(db_session, after.plan.id, "tranche_send_fenced")) == 1


async def test_i19_i24_a_fence_that_is_committed_before_a_retire_keeps_the_plan_unsettled(
    db_session, seed
) -> None:
    """Connection lost after the fence: the POST may still go out, so settlement waits."""
    r = await _ready(seed, db_session)
    out = await _commit(r)  # caller's session is gone from here on
    await ms.append_plan_event(
        db_session, plan_id=r.plan.id, event="retired", actor="operator:test"
    )
    await db_session.commit()
    state = await ms.plan_state(db_session, r.plan.id)
    assert state.unclosed_fences == (out.seq,)
    assert plan_fence_exposure(state) == Decimal("300000")
    with pytest.raises(ms.MandateError):
        await ms.append_plan_event(
            db_session,
            plan_id=r.plan.id,
            event="orders_settled",
            actor="system:mandate_projection",
        )
    await db_session.rollback()
    assert (await ms.plan_state(db_session, r.plan.id)).cleanup != "orders_clear"


async def test_the_pre_send_recheck_sees_a_retire_a_freeze_and_its_own_fence(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    out = await _commit(r)
    req = _req(r)

    async def recheck(**kw):
        return await fence.send_boundary_refusal(
            req, fenced_seq=out.seq, freeze_fn=r.freeze, **kw
        )

    assert await recheck() is None  # healthy: its own fence does not block it
    r.freeze.frozen = True
    assert await recheck() == R.DRAWDOWN_FROZEN.value
    r.freeze.frozen = False
    assert (
        await fence.send_boundary_refusal(req, fenced_seq=999, freeze_fn=r.freeze)
        == R.TRANCHE_UNCERTAIN.value
    )
    await ms.append_plan_event(
        db_session, plan_id=r.plan.id, event="suspended", actor="operator:test"
    )
    await db_session.commit()
    assert await recheck() == R.PLAN_NOT_ACTIVE.value


# ------------------------------------------------------------ X11 / X12 commit loss
async def test_x11_a_session_that_dies_after_the_insert_leaves_nothing(
    db_session, seed, monkeypatch
) -> None:
    r = await _ready(seed, db_session)
    real = fence._fence_once

    async def dies_after_insert(session, req, freeze_fn):
        await real(session, req, freeze_fn)
        raise ConnectionError("killed before commit")

    monkeypatch.setattr(fence, "_fence_once", dies_after_insert)
    out = await _commit(r)
    monkeypatch.undo()

    assert (out.fenced, out.refusal) == (False, R.FENCE_UNAVAILABLE.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []
    assert (await _commit(r)).fenced is True  # nothing was left behind to block it


class LosesTheCommitAnswer:
    """A real session whose commit succeeds but whose answer never arrives."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def commit(self) -> None:
        await self._inner.commit()
        raise ConnectionError("commit response lost")


async def test_x12_a_lost_commit_answer_sends_nothing_and_blocks_the_next_fence(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    out = await fence.commit_send_fence(
        _req(r),
        freeze_fn=r.freeze,
        session_factory=lambda: LosesTheCommitAnswer(AsyncSessionLocal()),
    )
    assert (out.fenced, out.refusal) == (False, R.FENCE_UNAVAILABLE.value)
    # the fence did commit: the tranche is blocked until a reconcile closes it
    assert len(await _events(db_session, r.plan.id, "tranche_send_fenced")) == 1
    nxt = await _commit(r)
    assert (nxt.fenced, nxt.refusal) == (False, R.TRANCHE_UNCERTAIN.value)


# ------------------------------------------------------------- X5 .. X9 profile case
async def test_x5_a_withdrawn_profile_commits_first_so_the_automatic_fence_is_refused(
    db_session, seed
) -> None:
    """r4618309267: judged with P1, session lost, P2 (max_tranche_index 0) committed."""
    r = await _ready(seed, db_session, profile_max=1)
    request = await _profile(db_session, r.sleeve, r.terms, 0)
    await _approve_profile(db_session, request)
    await db_session.commit()
    broker_calls = 0
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    if out.fenced:
        broker_calls += 1
    assert broker_calls == 0
    assert (out.fenced, out.refusal) == (False, R.ACTIVATION_MISSING.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_a_healthy_automatic_fence_cites_the_latest_profile_id(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    assert out.fenced is True
    (row,) = await _events(db_session, r.plan.id, "tranche_send_fenced")
    assert (row.approval_mode, row.activation_profile_id) == ("auto", r.profile.id)


async def test_an_automatic_fence_without_the_t5_cap_check_is_never_written(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    switch_only = fence.AutoFenceInputs(switch_enabled=True, judged_terms_id=r.terms.id)
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=switch_only), freeze_fn=r.freeze
    )
    assert out.fenced is False
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_x6_the_fence_wins_the_race_with_a_revoking_writer_and_posts_once(
    db_session, seed
) -> None:
    """The fence holds F2 shared; the revoking profile writer waits; the fence commits
    with P1; the next fence is refused."""
    r = await _ready(seed, db_session, profile_max=1)
    revoke = await _profile(db_session, r.sleeve, r.terms, 0)
    inside = asyncio.Event()
    go = asyncio.Event()

    async def hold_then_pass(session, facts):
        inside.set()
        await go.wait()
        return None

    fence_task = asyncio.create_task(
        fence.commit_send_fence(
            _req(r, mode="auto", auto=_auto(r, caps=hold_then_pass)),
            freeze_fn=r.freeze,
        )
    )
    await asyncio.wait_for(inside.wait(), 10)

    async def writer():
        async with AsyncSessionLocal() as s:
            await _approve_profile(s, revoke)
            await s.commit()

    writer_task = asyncio.create_task(writer())
    assert await _blocked_sessions(), "the profile writer must wait on F2"
    assert not writer_task.done()
    go.set()
    out = await asyncio.wait_for(fence_task, 10)
    await asyncio.wait_for(writer_task, 10)

    assert out.fenced is True  # P1 was the latest when F2 was taken
    (row,) = await _events(db_session, r.plan.id, "tranche_send_fenced")
    assert row.activation_profile_id == r.profile.id
    await fence.release_send_fence(r.plan.id, out.seq, reason_code="pre_send_refused")
    nxt = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    assert (nxt.fenced, nxt.refusal) == (False, R.ACTIVATION_MISSING.value)


async def test_x7_a_revoking_writer_that_holds_f2_makes_the_fence_wait_and_refuse(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    revoke = await _profile(db_session, r.sleeve, r.terms, 0)
    async with AsyncSessionLocal() as writer:
        await _approve_profile(writer, revoke)  # F2 bumped, not yet committed
        fence_task = asyncio.create_task(
            fence.commit_send_fence(
                _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
            )
        )
        assert await _blocked_sessions(), "the fence must wait on F2"
        assert not fence_task.done()
        await writer.commit()
    out = await asyncio.wait_for(fence_task, 10)
    assert (out.fenced, out.refusal) == (False, R.ACTIVATION_MISSING.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_x8_a_paused_sleeve_version_stops_the_automatic_fence_not_the_human_one(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    async with AsyncSessionLocal() as writer:
        await sleeve_store.set_sleeve_version(
            writer,
            sleeve_id=r.sleeve.id,
            state="paused",
            reason="x8 pause",
            actor="operator:test",
        )
        fence_task = asyncio.create_task(
            fence.commit_send_fence(
                _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
            )
        )
        assert await _blocked_sessions(), "the fence must wait on F2"
        await writer.commit()
    out = await asyncio.wait_for(fence_task, 10)
    assert out.refusal in {"sleeve_not_live", R.TERMS_MISSING.value}
    assert out.fenced is False
    # a person who approves a card after the pause is not stopped by the sleeve state
    assert (await _commit(r)).fenced is True


async def test_x8_a_new_clause_version_stops_the_automatic_fence(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    # a newer sleeve version without a clause: the plan's clause is no longer current
    await sleeve_store.set_sleeve_version(
        db_session,
        sleeve_id=r.sleeve.id,
        budget_cap="9000000",
        reason="x8 budget",
        actor="operator:test",
    )
    await db_session.commit()
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    assert (out.fenced, out.refusal) == (False, R.TERMS_MISSING.value)


async def test_x8_a_switch_that_is_off_stops_the_automatic_fence(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    off = fence.AutoFenceInputs(
        switch_enabled=False, judged_terms_id=r.terms.id, cap_check=_no_caps
    )
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=off), freeze_fn=r.freeze
    )
    assert (out.fenced, out.refusal) == (False, R.SWITCH_OFF.value)


async def test_x9_a_mutant_that_skips_f2_and_cites_a_stale_profile_is_refused_by_the_trigger(
    db_session, seed, monkeypatch
) -> None:
    r = await _ready(seed, db_session, profile_max=1)
    stale_id = r.profile.id
    await _approve_profile(db_session, await _profile(db_session, r.sleeve, r.terms, 0))
    await db_session.commit()

    async def no_f2(session, realm, basket_key):
        return 0

    async def stale_facts(session, req, loaded, state):
        from app.services.mandate.fence_rules import AutoFacts

        return (
            AutoFacts(
                switch_enabled=True,
                sleeve_live=True,
                live_terms_id=r.terms.id,
                live_terms_expired=False,
                plan_terms_id=r.terms.id,
                judged_terms_id=r.terms.id,
                profile_id=stale_id,
                profile_terms_id=r.terms.id,
                profile_max_tranche_index=1,
                profile_effective_ok=True,
                profile_expired=False,
                profile_approval_ok=True,
                tranche_index=1,
            ),
            stale_id,
        )

    monkeypatch.setattr(fence_locks, "lock_authority_shared", no_f2)
    monkeypatch.setattr(fence, "gather_auto_facts", stale_facts)
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    assert out.fenced is False
    assert out.refusal == R.ACTIVATION_MISSING.value
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_x16_another_sleeves_new_profile_neither_blocks_nor_rescues(
    db_session, seed
) -> None:
    key = f"b-{uuid.uuid4().hex[:10]}"
    sym = new_symbol()
    kis, kis_terms = await seed.clause_sleeve(
        symbols=(sym,), basket_key=key, account_mode="kis_live"
    )
    toss, toss_terms = await seed.clause_sleeve(
        symbols=(new_symbol(),), basket_key=key, account_mode="toss_live"
    )
    plan = seed.plan_snapshot((await seed.register(kis, kis_terms, sym)).plan)
    await seed.approve_plan(plan.id, kis_terms)
    kis_profile = await _approve_profile(
        db_session, await _profile(db_session, kis, kis_terms, 1)
    )
    await db_session.commit()
    proposal = await seed.bound_proposal(plan=plan, sleeve=kis)
    for event, fields in (
        ("tranche_armed", {"tranche_index": 1}),
        ("tranche_proposed", {"tranche_index": 1, "proposal_id": proposal.proposal_id}),
    ):
        await ms.append_plan_event(
            db_session, plan_id=plan.id, event=event, actor="o", **fields
        )
    await db_session.commit()
    r = SimpleNamespace(
        sleeve=kis,
        terms=kis_terms,
        plan=plan,
        pid=proposal.proposal_id,
        profile=kis_profile,
        freeze=Freeze(),
    )
    # Toss, the other sleeve of the basket, gets a new, allowing profile: the KIS plan
    # still has KIS's own (valid) profile and fences with it, not Toss's.
    toss_profile = await _approve_profile(
        db_session, await _profile(db_session, toss, toss_terms, 3)
    )
    await db_session.commit()
    out = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    assert out.fenced is True
    (row,) = await _events(db_session, plan.id, "tranche_send_fenced")
    assert row.activation_profile_id == kis_profile.id != toss_profile.id

    # now KIS revokes, Toss later issues another allowing profile: KIS stays refused
    await fence.release_send_fence(plan.id, out.seq, reason_code="pre_send_refused")
    await _approve_profile(db_session, await _profile(db_session, kis, kis_terms, 0))
    await db_session.commit()
    await _approve_profile(db_session, await _profile(db_session, toss, toss_terms, 3))
    await db_session.commit()
    again = await fence.commit_send_fence(
        _req(r, mode="auto", auto=_auto(r)), freeze_fn=r.freeze
    )
    assert (again.fenced, again.refusal) == (False, R.ACTIVATION_MISSING.value)


# ---------------------------------------------------------------- X10 / X13 / X15
async def test_x10_a_caller_that_holds_an_uncommitted_event_times_the_fence_out(
    db_session, seed, monkeypatch
) -> None:
    monkeypatch.setattr(fence, "FENCE_LOCK_TIMEOUT_MS", 500)
    r = await _ready(seed, db_session)
    async with AsyncSessionLocal() as caller:
        await ms.append_plan_event(
            caller, plan_id=r.plan.id, event="suspended", actor="operator:test"
        )  # flushed, not committed: its seq is the one the fence will want
        out = await _commit(r)
        await caller.rollback()
    assert (out.fenced, out.refusal) == (False, R.FENCE_UNAVAILABLE.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_x10_a_sleeve_version_writer_waits_on_l3_without_holding_f2(
    db_session, seed
) -> None:
    """The dispatch caller holds L3; the version writer waits there and takes no F2, so
    the fence (which needs F2) is not held up by it and nothing deadlocks."""
    r = await _ready(seed, db_session)
    async with AsyncSessionLocal() as dispatch:
        await sleeve_store.acquire_sleeve_lock(dispatch, r.sleeve.id)

        async def writer():
            async with AsyncSessionLocal() as s:
                await sleeve_store.set_sleeve_version(
                    s,
                    sleeve_id=r.sleeve.id,
                    budget_cap="9100000",
                    reason="x10 writer",
                    actor="operator:test",
                )
                await s.commit()

        writer_task = asyncio.create_task(writer())
        assert await _blocked_sessions(), "the writer must wait on the sleeve lock L3"
        out = await asyncio.wait_for(_commit(r), 10)  # F2 is free: no wait, no deadlock
        assert out.fenced is True
        assert not writer_task.done()
        await dispatch.commit()
    await asyncio.wait_for(writer_task, 10)


async def test_x13_a_freeze_writer_holding_f1_makes_the_fence_wait_then_refuse(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    async with AsyncSessionLocal() as freezer:
        await fence_locks.acquire_freeze_exclusive(freezer, "live", r.terms.basket_key)
        fence_task = asyncio.create_task(_commit(r))
        assert await _blocked_sessions(), "the fence must wait on F1"
        assert not fence_task.done()
        r.freeze.frozen = True  # the freeze is committed with the lock release
        await freezer.commit()
    out = await asyncio.wait_for(fence_task, 10)
    assert (out.fenced, out.refusal) == (False, R.DRAWDOWN_FROZEN.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []


async def test_x13_a_fence_that_commits_before_the_freeze_is_allowed_one_post(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session)
    out = await _commit(r)
    assert out.fenced is True
    async with AsyncSessionLocal() as freezer:
        await fence_locks.acquire_freeze_exclusive(freezer, "live", r.terms.basket_key)
        r.freeze.frozen = True
        await freezer.commit()
    assert (await _commit(r)).fenced is False  # no second fence once frozen
    assert len(await _events(db_session, r.plan.id, "tranche_send_fenced")) == 1


async def test_x15_every_basket_has_an_authority_row_and_a_missing_one_blocks_the_fence(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=None)
    count = (
        await db_session.execute(
            sa.text(
                "SELECT count(*) FROM review.mandate_basket_authority "
                "WHERE realm = 'live' AND basket_key = :k"
            ),
            {"k": r.terms.basket_key},
        )
    ).scalar_one()
    assert count == 1
    await db_session.execute(sa.text("SET LOCAL session_replication_role = replica"))
    await db_session.execute(
        sa.text("DELETE FROM review.mandate_basket_authority WHERE basket_key = :k"),
        {"k": r.terms.basket_key},
    )
    await db_session.commit()

    out = await _commit(r)
    assert (out.fenced, out.refusal) == (False, R.UNREADABLE.value)
    assert await _events(db_session, r.plan.id, "tranche_send_fenced") == []
    # a profile writer whose bump updates zero rows fails and writes nothing
    request = await _profile(db_session, r.sleeve, r.terms, 1)
    with pytest.raises(ms.MandateError) as caught:
        await _approve_profile(db_session, request)
    assert caught.value.code is R.UNREADABLE
    await db_session.rollback()
    assert (await ms.latest_profile(db_session, r.sleeve.id)) is None
    # a sleeve-version writer fails the same way
    with pytest.raises(fence_locks.AuthorityRowMissing):
        await sleeve_store.set_sleeve_version(
            db_session,
            sleeve_id=r.sleeve.id,
            budget_cap="9200000",
            reason="x15",
            actor="operator:test",
        )
    await db_session.rollback()


async def test_each_writer_of_a_fence_fact_counts_the_basket_authority_up(
    db_session, seed
) -> None:
    sym = new_symbol()
    sleeve, terms = await seed.clause_sleeve(symbols=(sym,))

    async def seq() -> int:
        await db_session.rollback()
        return (
            await db_session.execute(
                sa.text(
                    "SELECT authority_seq FROM review.mandate_basket_authority "
                    "WHERE realm = 'live' AND basket_key = :k"
                ),
                {"k": terms.basket_key},
            )
        ).scalar_one()

    after_clause = await seq()
    assert after_clause >= 1  # the clause approval created and bumped the row
    await _approve_profile(db_session, await _profile(db_session, sleeve, terms, 1))
    await db_session.commit()
    after_profile = await seq()
    assert after_profile == after_clause + 1
    await seed.register(sleeve, terms, sym)
    after_plan = await seq()
    assert after_plan == after_profile + 1
    await sleeve_store.set_sleeve_version(
        db_session,
        sleeve_id=sleeve.id,
        budget_cap="9300000",
        reason="counts",
        actor="operator:test",
    )
    await db_session.commit()
    assert await seq() == after_plan + 1


async def test_a_sleeve_without_a_mandate_clause_is_untouched_by_the_authority_writer(
    db_session, seed
) -> None:
    plain = await seed.sleeve(
        state="shadow",
        budget_cap="1000000",
        loss_limit="100000",
        account_mode="kis_live",
        market="equity_kr",
        rule_id="at3795.basket1_value_split_entry",
    )
    before = (
        await db_session.execute(
            sa.text(
                "SELECT count(*), coalesce(sum(authority_seq), 0) FROM review.mandate_basket_authority"
            )
        )
    ).one()
    await sleeve_store.set_sleeve_version(
        db_session,
        sleeve_id=plain.id,
        budget_cap="1100000",
        reason="no basket",
        actor="operator:test",
    )
    await db_session.commit()
    after = (
        await db_session.execute(
            sa.text(
                "SELECT count(*), coalesce(sum(authority_seq), 0) FROM review.mandate_basket_authority"
            )
        )
    ).one()
    assert tuple(before) == tuple(after)


async def test_the_coordination_tables_refuse_delete_truncate_and_value_changes(
    db_session, seed
) -> None:
    r = await _ready(seed, db_session, profile_max=None)
    key = {"k": r.terms.basket_key}
    for statement, params in (
        ("DELETE FROM review.mandate_basket_authority WHERE basket_key = :k", key),
        (
            "UPDATE review.mandate_basket_authority SET authority_seq = authority_seq + 2 "
            "WHERE basket_key = :k",
            key,
        ),
        (
            "UPDATE review.mandate_basket_authority SET authority_seq = 0 "
            "WHERE basket_key = :k",
            key,
        ),
        ("TRUNCATE review.mandate_basket_authority", {}),
        ("TRUNCATE review.mandate_cap_mutex", {}),
    ):
        with pytest.raises(sa.exc.DBAPIError):
            await db_session.execute(sa.text(statement), params)
        await db_session.rollback()
    await fence_locks.lock_mutex(db_session, "sleeve:0", "sleeve")
    await db_session.commit()
    for statement in (
        "UPDATE review.mandate_cap_mutex SET scope = 'global' WHERE mutex_key = 'sleeve:0'",
        "DELETE FROM review.mandate_cap_mutex WHERE mutex_key = 'sleeve:0'",
    ):
        with pytest.raises(sa.exc.DBAPIError):
            await db_session.execute(sa.text(statement))
        await db_session.rollback()
    await db_session.execute(sa.text("SET LOCAL session_replication_role = replica"))
    await db_session.execute(
        sa.text("DELETE FROM review.mandate_cap_mutex WHERE mutex_key = 'sleeve:0'")
    )
    await db_session.commit()


async def test_a_released_fence_leaves_no_exposure_and_a_filled_one_keeps_its_fill(
    db_session, seed
) -> None:
    """X25/X26 (human side): the plan's side of the exposure contract on real events."""
    r = await _ready(seed, db_session)
    first = await _commit(r)
    await fence.release_send_fence(r.plan.id, first.seq, reason_code="pre_send_refused")
    assert plan_fence_exposure(await ms.plan_state(db_session, r.plan.id)) == 0
    second = await _commit(r)  # a new attempt row for the re-approval
    assert second.fenced is True and second.seq != first.seq
    await ms.append_plan_event(
        db_session,
        plan_id=r.plan.id,
        event="tranche_partially_filled",
        actor="system:mandate_projection",
        tranche_index=1,
        proposal_id=r.pid,
        fill_amount=Decimal("300000"),
        fill_quantity=Decimal("3"),
        evidence={"trade_date": "2026-11-23"},
    )
    await db_session.commit()
    state = await ms.plan_state(db_session, r.plan.id)
    assert state.unclosed_fences == ()
    assert plan_fence_exposure(state) == Decimal("300000")
