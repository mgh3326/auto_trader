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
from decimal import Decimal
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
# R8 B14 + SHOULD 3 — the positive paths, through the production default seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_approval_traverses_the_real_core_to_a_real_submission(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 B14 — an *active* ``op``, end to end, no ``handler=`` override.

    Every other production-default test steers into a fail-closed gate. This
    one lets a live approval through: only the broker leg is a fake, and it is
    an exact counter. A worker that shortcut valid approvals through some
    other mutation route would show up here as a broker call that never
    happened, or a proposal that never moved.
    """
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.revalidation import RungOutcome

    group = await seed_proposal(db_session, nonce="activeop123", symbol="ACTKR")
    data = proposal_callback_data(group, action="op")
    job_id = await _queue(inbox_cleanup, data)

    submissions: list[tuple[uuid.UUID, Any]] = []

    async def _fake_broker(*, service, proposal_id, now, **kwargs):
        submissions.append((proposal_id, now))
        return [RungOutcome(0, "submitted_acked", {})]

    monkeypatch.setattr(callback_module, "revalidate_and_submit", _fake_broker)

    entered: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        entered.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    notifier = FakeNotifier()
    result = await _run_default_seam(job_id, notifier, monkeypatch)

    # -- the real core, once, with the real action --------------------------
    assert entered == ["op"]
    assert result["status"] == "succeeded", result

    # -- the gated broker leg ran exactly once, for this proposal -----------
    assert len(submissions) == 1, submissions
    assert submissions[0][0] == group.proposal_id

    # -- and the proposal really moved --------------------------------------
    async with AsyncSessionLocal() as session:
        refreshed, rungs = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
    assert refreshed.approval_nonce_used_at is not None, "the nonce was not consumed"
    assert refreshed.approved_at is not None
    assert refreshed.approved_by_telegram_user_id == "777"
    assert [rung.state for rung in rungs] == ["acked"]

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.outcome == "approved"

    # -- a redelivery must not submit a second time -------------------------
    replay_job = await _queue(inbox_cleanup, data)
    replay = await _run_default_seam(replay_job, FakeNotifier(), monkeypatch)
    assert replay["status"] == "discarded"
    assert len(submissions) == 1, "a replay reached the broker leg"


@pytest.mark.asyncio
async def test_an_auto_veto_reaches_its_own_cancel_branch(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 SHOULD 3 — ``vc`` reaches the cancel branch, not a shared preflight."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.target_order import TargetOrderSnapshot

    group = await seed_auto_veto_proposal(db_session, nonce="vetolive123")
    data = proposal_callback_data(group, action="vc")
    job_id = await _queue(inbox_cleanup, data)

    cancelled: list[str] = []

    async def _fake_cancel(*args, **kwargs):
        cancelled.append("cancel")
        return {"success": True}

    async def _fake_fetch(*args, **kwargs):
        return TargetOrderSnapshot(
            broker_order_id="broker-x",
            status="cancelled",
            filled_quantity=Decimal("0"),
            remaining_quantity=Decimal("0"),
            raw={},
        )

    monkeypatch.setattr(callback_module, "cancel_target_order", _fake_cancel)
    monkeypatch.setattr(callback_module, "fetch_target_order", _fake_fetch)

    entered: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        entered.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    await _run_default_seam(job_id, FakeNotifier(), monkeypatch)

    assert entered == ["vc"]
    assert cancelled == ["cancel"], "the veto action did not reach the cancel branch"


@pytest.mark.asyncio
async def test_a_loss_cut_first_click_reaches_its_own_preview_branch(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R8 SHOULD 3 — ``lc`` opens the two-click ceremony, not the approve path."""
    from app.services.order_proposals import revalidation as revalidation_module
    from app.services.order_proposals.callback_inbox import worker as worker_module

    group = await seed_loss_cut_proposal(db_session, monkeypatch, nonce="losslive12")
    data = proposal_callback_data(group, action="lc")
    job_id = await _queue(inbox_cleanup, data)

    previews: list[int] = []

    async def _fake_preview(**kwargs):
        previews.append(1)
        return {
            "rungs": [
                {
                    "rung_index": 0,
                    "current_price": "100",
                    "avg_buy_price": "200",
                    "loss_pct": "-50.00",
                    "loss_cut_slip_band": "98",
                }
            ],
            "retrospective_id": 42,
            "lesson_excerpt": "손절 기준을 늦추지 않는다",
        }

    monkeypatch.setattr(
        revalidation_module, "preview_loss_cut_confirmation", _fake_preview
    )

    submissions: list[int] = []

    async def _never_submit(**kwargs):  # pragma: no cover - must not run
        submissions.append(1)
        return []

    monkeypatch.setattr(callback_module, "revalidate_and_submit", _never_submit)

    entered: list[str] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        entered.append(normalized.callback.action)
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    await _run_default_seam(job_id, FakeNotifier(), monkeypatch)

    assert entered == ["lc"]
    assert previews == [1], "the loss-cut action did not reach its preview branch"
    assert submissions == [], "a first click submitted without the second"
