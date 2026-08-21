"""W5 — every action survives the round trip with its own meaning.

Adversarial review R5, blocker 7. Everything else in this package exercises
``op``. The parser's vocabulary is ``{op, dn, lc, vc, ba}``, and the callback
core routes on it: ``dn`` denies, ``vc`` cancels an auto-submitted order, ``lc``
opens the loss-cut two-click ceremony, ``ba`` approves a batch. A worker that
rebuilt every stored envelope as ``op`` would turn a **deny** into an
**approval**, and every existing test in this package would stay green.

Two layers here:

1. a table over all five actions proving the stored row rebuilds into exactly
   the envelope that was parsed -- action, subject and the whole binding;
2. behavioural runs through the **production default seam** (the real TaskIQ
   task, no ``handler=`` override) proving each action reaches its own branch
   of the callback core and keeps its own fail-closed behaviour.

The broker is never reached: ``dn`` is a DB-only path by construction, and the
``lc``/``vc``/``ba`` cases are steered into their existing fail-closed gates,
which the core guarantees make no external call at all.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import approval_message as approval_messages
from app.services.order_proposals import telegram_callback as callback_module
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    DispatchBinding,
    build_membership_digest,
)

from .conftest import (
    CHAT_ID,
    FakeNotifier,
    consume_nonce,
    load_job,
    make_update,
    proposal_callback_data,
    seed_approval_batch,
    seed_auto_veto_proposal,
    seed_loss_cut_proposal,
    seed_proposal,
)

pytestmark = pytest.mark.integration

ALL_ACTIONS = ("op", "dn", "lc", "vc", "ba")


async def _queue(inbox_cleanup: list[uuid.UUID], data: str) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 610_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(data=data, update_id=update_id, callback_id=f"cbq-{update_id}"),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.accepted is True, result
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


def _synthetic(action: str) -> str:
    """Callback data for one action, built by the production builder."""
    subject_id = uuid.uuid4()
    nonce = f"n{uuid.uuid4().hex[:10]}"
    card_kind = {
        "op": ApprovalCardKind.MANUAL,
        "dn": ApprovalCardKind.MANUAL,
        "lc": ApprovalCardKind.LOSS_CUT_CONFIRMATION,
        "vc": ApprovalCardKind.AUTO_VETO,
        "ba": ApprovalCardKind.BATCH,
    }[action]
    return approval_messages.build_callback_data(
        action=action,
        proposal_id=subject_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=card_kind,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=card_kind,
                membership_revision=1,
                members=[{"proposal_id": str(subject_id), "approval_nonce": nonce}],
            ),
        ),
    )


# ---------------------------------------------------------------------------
# 1. the stored row rebuilds into exactly what was parsed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ALL_ACTIONS)
async def test_every_action_survives_normalize_commit_and_rebuild(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], action: str
) -> None:
    """R5 B7 — the durable round trip is identity on the envelope."""
    from app.services.order_proposals.callback_inbox.worker import rebuild_normalized

    data = _synthetic(action)
    parsed = callback_module.parse_callback_data(data)
    assert parsed.action == action

    job_id = await _queue(inbox_cleanup, data)
    row = await load_job(job_id)
    assert row is not None
    assert row.action == action, "the stored action drifted from the parsed one"

    rebuilt = rebuild_normalized(row)
    assert rebuilt.callback == parsed, (
        f"rebuilding a {action!r} job produced a different envelope"
    )
    assert rebuilt.callback.action == action
    assert rebuilt.chat_id_key == str(CHAT_ID)


@pytest.mark.asyncio
async def test_the_five_actions_produce_five_distinct_stored_actions(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """A collapse to a single action is visible as a set of size 1."""
    stored: list[str] = []
    for action in ALL_ACTIONS:
        job_id = await _queue(inbox_cleanup, _synthetic(action))
        row = await load_job(job_id)
        assert row is not None
        stored.append(row.action)
    assert stored == list(ALL_ACTIONS)
    assert len(set(stored)) == 5


# ---------------------------------------------------------------------------
# 2. each action reaches its own branch of the real core
# ---------------------------------------------------------------------------


async def _run_default_seam(
    job_id: uuid.UUID, notifier: FakeNotifier, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """The production path: the real task, no handler override."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)
    return await task_module.run_telegram_callback_job(str(job_id))


@pytest.mark.asyncio
async def test_deny_denies_and_never_becomes_an_approval(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R5 B7, the one that matters most.

    A ``dn`` job run through the production default seam must reject the
    rungs. Deny is DB-only in the callback core -- it never calls
    ``revalidate_and_submit`` -- so the whole path can run for real here, and
    a worker that reconstructed ``dn`` as ``op`` would instead have submitted.
    """
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_proposal(db_session, nonce="denyactn123", symbol="DNYKR")
    data = proposal_callback_data(group, action="dn")
    assert callback_module.parse_callback_data(data).action == "dn"
    job_id = await _queue(inbox_cleanup, data)

    seen: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        seen.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    notifier = FakeNotifier()
    result = await _run_default_seam(job_id, notifier, monkeypatch)

    assert seen == ["dn"], "the worker handed the core a different action"
    assert result["status"] == "succeeded"

    row = await load_job(job_id)
    assert row is not None
    assert row.outcome == "denied", row.outcome

    async with AsyncSessionLocal() as session:
        refreshed, rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    # Denied, not approved: no rung was ever handed to the broker.
    assert [rung.state for rung in rungs] == ["rejected"]
    assert all(rung.broker_order_id is None for rung in rungs)
    assert refreshed.approved_at is None
    assert refreshed.approval_nonce_used_at is not None


@pytest.mark.asyncio
async def test_an_auto_veto_action_reaches_the_veto_branch_and_fails_closed(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vc`` keeps its own gate; a spent nonce stops before any broker call."""
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_auto_veto_proposal(db_session, nonce="vetoactn123")
    data = proposal_callback_data(group, action="vc")
    job_id = await _queue(inbox_cleanup, data)
    await consume_nonce(group.proposal_id)

    seen: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        seen.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    notifier = FakeNotifier()
    result = await _run_default_seam(job_id, notifier, monkeypatch)

    assert seen == ["vc"]
    assert result["status"] == "discarded"
    row = await load_job(job_id)
    assert row is not None
    assert row.outcome == "nonce_replay"
    assert notifier.external_calls == 0

    # The resting order is untouched: no cancel was attempted.
    async with AsyncSessionLocal() as session:
        _refreshed, rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    assert [rung.state for rung in rungs] == ["resting"]


@pytest.mark.asyncio
async def test_a_loss_cut_action_reaches_the_loss_cut_branch_and_fails_closed(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``lc`` keeps the two-click ceremony's own gate."""
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_loss_cut_proposal(db_session, monkeypatch, nonce="lossactn12")
    data = proposal_callback_data(group, action="lc")
    job_id = await _queue(inbox_cleanup, data)
    await consume_nonce(group.proposal_id)

    seen: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        seen.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    notifier = FakeNotifier()
    result = await _run_default_seam(job_id, notifier, monkeypatch)

    assert seen == ["lc"]
    assert result["status"] == "discarded"
    row = await load_job(job_id)
    assert row is not None
    assert row.outcome == "nonce_replay"
    assert notifier.external_calls == 0


@pytest.mark.asyncio
async def test_a_batch_action_reaches_the_batch_branch_and_fails_closed(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``ba`` keeps the batch TTL/nonce gate, and its subject is the batch."""
    from sqlalchemy import text

    from app.services.order_proposals.callback_inbox import worker as worker_module

    batch, groups, data = await seed_approval_batch(db_session)
    parsed = callback_module.parse_callback_data(data)
    assert parsed.action == "ba"
    assert parsed.subject_short == str(batch.batch_id)[:8]

    job_id = await _queue(inbox_cleanup, data)

    # Spend the batch approval out of band, exactly as a first click would.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review.order_proposal_approval_batches "
                "SET approval_nonce_used_at = now() WHERE batch_id = :bid"
            ),
            {"bid": batch.batch_id},
        )
        await session.commit()

    seen: list[Any] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        seen.append((normalized.callback.action, normalized.callback.subject_short))
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    notifier = FakeNotifier()
    result = await _run_default_seam(job_id, notifier, monkeypatch)

    assert seen == [("ba", str(batch.batch_id)[:8])]
    assert result["status"] == "discarded"
    row = await load_job(job_id)
    assert row is not None
    assert row.outcome == "approval_batch_nonce_replay", row.outcome
    assert notifier.external_calls == 0

    # No member proposal moved.
    async with AsyncSessionLocal() as session:
        service = OrderProposalsService(session)
        for group in groups:
            _refreshed, rungs = await service.get_proposal(group.proposal_id)
            assert [rung.state for rung in rungs] == ["pending_approval"]


# ---------------------------------------------------------------------------
# R8 B14 + SHOULD 3 / R15 — the positive paths, through the *whole* real core
# ---------------------------------------------------------------------------
#
# R15's correction, and it matters: faking ``revalidate_and_submit`` would skip
# the approval hash, the fresh preview, the buying-power claim, the nonce and
# commit lease, and every rung transition -- precisely the machinery B14
# exists to prove the durable path reaches. So it is real here.
#
# REAL below the TaskIQ task: process_callback_job, the advisory lock, the
# attempt/entry markers, handle_normalized_callback, the published-binding
# preflight, nonce consumption, the commit lease, record_approval,
# revalidate_and_submit (fresh preview, buying-power claim, approval-hash
# plumbing, _classify_submit, every rung transition) and
# _default_place_order_fn -- all against the real test database.
#
# FAKED: order_execution._place_order_impl, plus the notifier.
#
# HONEST LIMIT (R16). ``_place_order_impl`` is not merely transport: it owns
# the input/market/account and env+confirm gates, approval-hash verification,
# the KIS/Toss/Upbit execution branch, the pre-send intent and idempotency
# boundary, the kis_mock attribution gate, and the accepted-only ledger
# service write. Faking it leaves all of that OUTSIDE this test. Those
# boundaries have their own suites (tests/test_mcp_kis_order_variants.py,
# tests/test_kis_mock_order_ledger.py, tests/test_live_loss_sell_hard_guard.py
# and the approval-hash suites) and W5 changes none of them. Pushing this test
# below _place_order_impl -- faking the client _create_kis_client returns, per
# R16 -- is a real and worthwhile extension. It is NOT done here, and this
# comment exists so nobody reads the assertions below as covering it.


class _BrokerTransport:
    """The lowest-level broker call, and nothing above it.

    Records every preview (``dry_run=True``) and every mutation
    (``dry_run=False``) separately, because "how many times did we actually
    send" and "did a fresh preview happen before we sent" are different
    questions and B14 asks both.
    """

    def __init__(self, *, price: str = "100", quantity: str = "10") -> None:
        self.previews: list[dict[str, Any]] = []
        self.mutations: list[dict[str, Any]] = []
        self._price = price
        self._quantity = quantity

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("dry_run") is True:
            self.previews.append(kwargs)
            return {
                "success": True,
                "dry_run": True,
                "price": float(self._price),
                "quantity": float(self._quantity),
                "approval_hash": "w5-approval-hash",
            }
        self.mutations.append(kwargs)
        return {
            "success": True,
            "dry_run": False,
            "broker_status": "accepted",
            "order_id": f"w5-broker-{len(self.mutations)}",
            "correlation_id": kwargs.get("correlation_id"),
        }


@pytest.mark.asyncio
async def test_a_valid_approval_traverses_the_real_core_to_a_real_submission(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 B14 / R15 — an *active* ``op``, end to end, real revalidation core.

    No ``handler=`` override and no ``revalidate_fn`` override: the production
    TaskIQ task drives the worker, which drives the real
    ``handle_normalized_callback``, which drives the real
    ``revalidate_and_submit``. Only the broker transport is a fake.
    """
    from app.mcp_server.tooling import order_execution
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_proposal(db_session, nonce="activeop123", symbol="ACTKR")
    data = proposal_callback_data(group, action="op")
    job_id = await _queue(inbox_cleanup, data)

    transport = _BrokerTransport()
    monkeypatch.setattr(order_execution, "_place_order_impl", transport)

    entered: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        entered.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    notifier = FakeNotifier()
    result = await _run_default_seam(job_id, notifier, monkeypatch)

    # -- the real core, once, with the real action --------------------------
    assert entered == ["op"], entered
    assert result["status"] == "succeeded", result

    # -- a fresh preview happened, and exactly one mutation was sent --------
    assert len(transport.previews) == 1, transport.previews
    assert len(transport.mutations) == 1, transport.mutations
    mutation = transport.mutations[0]
    assert mutation["symbol"] == "ACTKR"
    assert mutation["side"] == "buy"
    assert mutation.get("dry_run") is False
    # The preview really preceded the mutation.
    assert transport.previews[0].get("dry_run") is True

    # -- nonce and approval consumed exactly once, by the real gates --------
    async with AsyncSessionLocal() as session:
        refreshed, rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    assert refreshed.approval_nonce_used_at is not None, "the nonce was not consumed"
    assert refreshed.approved_at is not None
    assert refreshed.approved_by_telegram_user_id == "777"
    assert refreshed.commit_lease_until is not None, "no commit lease was taken"

    # -- the real rung transition and broker evidence, written by the core --
    assert [rung.state for rung in rungs] == ["resting"], [r.state for r in rungs]
    rung = rungs[0]
    assert rung.broker_order_id == "w5-broker-1"
    assert rung.correlation_id, "the core recorded no correlation id"
    assert rung.approval_hash_digest, "the core recorded no approval-hash digest"

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.outcome == "approved"

    # -- a redelivery mutates zero more times -------------------------------
    replay_job = await _queue(inbox_cleanup, data)
    replay = await _run_default_seam(replay_job, FakeNotifier(), monkeypatch)
    assert replay["status"] == "discarded"
    assert len(transport.mutations) == 1, "a replay reached the broker transport"

    # -- and neither does a recovery sweep over both jobs -------------------
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    await recover_callback_jobs()
    assert len(transport.mutations) == 1, "recovery re-sent a completed job"


@pytest.mark.asyncio
async def test_an_auto_veto_routes_to_the_veto_branch_not_the_approve_branch(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``vc`` reaches the veto branch, and never the approve branch.

    Scope, stated plainly: this pins *routing* and the fail-closed outcome,
    not a completed cancel. Driving a veto through its own broker legs needs
    the hermetic client fixture R16 describes, which this file does not build.
    What is asserted is that a ``vc`` enters the core as a ``vc``, that the
    approve branch's sender is never reached, and that the resting rung is
    left exactly as it was.
    """
    from app.mcp_server.tooling import order_execution
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_auto_veto_proposal(db_session, nonce="vetolive123")
    data = proposal_callback_data(group, action="vc")
    job_id = await _queue(inbox_cleanup, data)

    transport = _BrokerTransport()
    monkeypatch.setattr(order_execution, "_place_order_impl", transport)

    entered: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        entered.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    await _run_default_seam(job_id, FakeNotifier(), monkeypatch)

    assert entered == ["vc"], entered
    assert transport.mutations == [], "a veto reached the approve branch's sender"
    assert transport.previews == [], "a veto previewed an order"

    async with AsyncSessionLocal() as session:
        _after, after_rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    assert [rung.state for rung in after_rungs] == ["resting"], [
        rung.state for rung in after_rungs
    ]


@pytest.mark.asyncio
async def test_a_loss_cut_first_click_never_sends_an_order(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first click on a loss cut opens the ceremony; it must not send.

    The first click is an ``op`` on a loss-cut proposal (a MANUAL card); ``lc``
    is the *second* click. The core routes on ``group.exit_intent``, so this
    is the branch that must reach no sender at all.
    """
    from app.mcp_server.tooling import order_execution
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_loss_cut_proposal(
        db_session,
        monkeypatch,
        nonce="lossfirst1",
        card_kind=ApprovalCardKind.MANUAL,
    )
    data = proposal_callback_data(group, action="op")
    job_id = await _queue(inbox_cleanup, data)

    transport = _BrokerTransport(price="99", quantity="1")
    monkeypatch.setattr(order_execution, "_place_order_impl", transport)

    entered: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        entered.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    await _run_default_seam(job_id, FakeNotifier(), monkeypatch)

    assert entered == ["op"]
    assert transport.mutations == [], "a loss-cut first click sent an order"

    async with AsyncSessionLocal() as session:
        after, after_rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    assert [rung.state for rung in after_rungs] == ["pending_approval"], [
        rung.state for rung in after_rungs
    ]
    assert after.approval_nonce is not None
