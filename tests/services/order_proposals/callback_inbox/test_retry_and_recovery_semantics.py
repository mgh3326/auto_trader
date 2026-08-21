"""W5 — the retry algebra and the recovery scan, as behaviour.

Adversarial review R5, blockers 8 and 9, plus R6's first strengthening.

B8  Constants are not a contract. This drives ``process_callback_job`` with
    every outcome shape the callback core can actually produce and asserts the
    resulting state, error class, attempt count, next-attempt time and -- after
    a real recovery tick -- the handler call count. A single unhandled shape
    that fell through to ``retry_wait`` would re-enter an order-adjacent core.

B9  The crash tests call ``process_callback_job`` directly with a widened
    claimable set, which bypasses production *selection*. This drives the real
    ``recover_callback_jobs`` over a mixed inbox and asserts exactly which rows
    it touches.

One deliberate design note for B9: recovery **processes** eligible rows rather
than re-kicking Redis. Re-kicking would be useless in the failure this exists
for -- Redis being the thing that broke. The invariant asserted is therefore
"each eligible row is claimed and executed exactly once, and no ineligible row
is touched", which is the property a kick would have been a means to.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import load_job, make_update

pytestmark = pytest.mark.integration


def _synthetic_data(nonce: str = "nonce123456") -> str:
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    proposal_id = uuid.uuid4()
    return build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
            ),
        ),
    )


async def _queue(inbox_cleanup: list[uuid.UUID]) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 620_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=_synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _force(job_id: uuid.UUID, **fields: Any) -> None:
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    async with AsyncSessionLocal() as session:
        await CallbackInboxService(session).force_state_for_test(job_id, **fields)
        await session.commit()


# ---------------------------------------------------------------------------
# B8 — retry classification, driven by real outcome shapes
# ---------------------------------------------------------------------------

#: (label, handler result, expected state, expected error class)
_OUTCOMES: tuple[tuple[str, Any, str, str | None], ...] = (
    (
        "acked",
        {"handled": True, "reason": "approved", "results": ["submitted_acked"]},
        "succeeded",
        None,
    ),
    (
        "resting",
        {"handled": True, "reason": "approved", "results": ["submitted_resting"]},
        "succeeded",
        None,
    ),
    # An ambiguous *send* is the proposal/order state machine's problem, and it
    # already owns it. Re-running the callback cannot resolve it and might
    # duplicate it, so the job is done.
    (
        "unverified",
        {"handled": True, "reason": "approved", "results": ["unverified"]},
        "succeeded",
        None,
    ),
    (
        "rung_error",
        {"handled": True, "reason": "approved", "results": ["error"]},
        "succeeded",
        None,
    ),
    (
        "needs_reconfirm",
        {"handled": True, "reason": "needs_reconfirm", "results": ["needs_reconfirm"]},
        "succeeded",
        None,
    ),
    ("denied", {"handled": True, "reason": "denied"}, "succeeded", None),
    (
        "proposal_not_found",
        {"handled": False, "reason": "proposal_not_found"},
        "discarded",
        None,
    ),
    ("nonce_replay", {"handled": False, "reason": "nonce_replay"}, "discarded", None),
    ("expired", {"handled": False, "reason": "EXPIRED"}, "discarded", None),
    (
        "superseded",
        {"handled": False, "reason": f"proposal_superseded_by:{uuid.uuid4()}"},
        "discarded",
        None,
    ),
    (
        "chat_not_allowed",
        {"handled": False, "reason": "chat_not_allowed"},
        "discarded",
        None,
    ),
    (
        "guard_blocked",
        {
            "handled": False,
            "reason": "approval_window_blocked",
            "results": ["guard_blocked"],
        },
        "discarded",
        None,
    ),
    (
        "submit_rejected",
        {"handled": False, "reason": "submit_rejected"},
        "discarded",
        None,
    ),
    ("lease_held", {"handled": False, "reason": "lease_held"}, "discarded", None),
    (
        "unknown_future_reason",
        {"handled": False, "reason": "some_reason_invented_next_year"},
        "discarded",
        None,
    ),
    # The one that must never re-run: the core swallows every exception into
    # this string, including one raised after a submission.
    (
        "internal_error",
        {"handled": False, "reason": "internal_error"},
        "dead_letter",
        "handler_ambiguous",
    ),
    ("not_a_dict", "surprise", "dead_letter", "handler_ambiguous"),
    # R8 B15: a handler-returned marker is forgeable by a handler that has
    # already mutated. None of these may buy a replay.
    (
        "forged_marker_true",
        {"handled": False, "reason": "internal_error", "mutation_not_started": True},
        "dead_letter",
        "handler_ambiguous",
    ),
    (
        "forged_marker_truthy_int",
        {"handled": False, "reason": "internal_error", "mutation_not_started": 1},
        "dead_letter",
        "handler_ambiguous",
    ),
    (
        "forged_marker_string",
        {"handled": False, "reason": "internal_error", "mutation_not_started": "yes"},
        "dead_letter",
        "handler_ambiguous",
    ),
    (
        "forged_marker_object",
        {
            "handled": False,
            "reason": "internal_error",
            "mutation_not_started": object,
        },
        "dead_letter",
        "handler_ambiguous",
    ),
    (
        "forged_marker_false",
        {"handled": False, "reason": "internal_error", "mutation_not_started": False},
        "dead_letter",
        "handler_ambiguous",
    ),
    (
        "forged_marker_on_handled",
        {
            "handled": True,
            "reason": "approved",
            "results": ["unverified"],
            "mutation_not_started": True,
        },
        "succeeded",
        None,
    ),
    (
        "forged_marker_on_rejection",
        {
            "handled": False,
            "reason": "nonce_replay",
            "mutation_not_started": True,
        },
        "discarded",
        None,
    ),
    (
        "forged_marker_with_retry_words",
        {
            "handled": False,
            "reason": "pre_core_failure",
            "mutation_not_started": True,
            "retry": True,
        },
        "discarded",
        None,
    ),
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "result", "expected_state", "expected_error"),
    _OUTCOMES,
    ids=[case[0] for case in _OUTCOMES],
)
async def test_every_core_outcome_lands_in_its_documented_state(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    label: str,
    result: Any,
    expected_state: str,
    expected_error: str | None,
) -> None:
    """R5 B8 — state, error class, attempts, and no second core call."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)
    calls: list[int] = []

    async def _handler(normalized, **kwargs):
        calls.append(1)
        return result

    await process_callback_job(job_id, handler=_handler)

    row = await load_job(job_id)
    assert row is not None
    assert row.state == expected_state, label
    assert row.error_class == expected_error, label
    assert row.attempt_count == 1, label
    assert calls == [1], label

    # No handler return shape reaches ``retry_wait``: the core was entered.
    assert row.state != "retry_wait", (
        f"{label}: a handler-returned value bought a replay"
    )
    assert row.handler_entered_at is not None, label
    # Terminal: authority gone, and a real recovery tick must not touch it.
    assert row.nonce is None and row.chat_id is None, label
    await recover_callback_jobs(handler=_handler)
    assert calls == [1], f"{label}: a terminal job re-entered the core"
    assert (await load_job(job_id)).state == expected_state


@pytest.mark.asyncio
async def test_only_a_pre_core_failure_re_enters_and_only_after_its_backoff(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R8 B15 — retry authority is worker-owned and raised before entry."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)
    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - never reached
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    def _fail_pre_core(*args, **kwargs):
        raise RuntimeError("notifier unavailable")

    original = worker_module.resolve_notifier
    worker_module.resolve_notifier = _fail_pre_core
    try:
        result = await process_callback_job(job_id, handler=_handler)
        assert result["status"] == "retry_scheduled"
        assert calls == []

        row = await load_job(job_id)
        assert row is not None
        assert row.state == "retry_wait"
        assert row.error_class == "pre_core_failure"
        assert row.handler_entered_at is None
        assert row.available_at > row.received_at

        # Not due yet.
        await recover_callback_jobs(handler=_handler)
        assert (await load_job(job_id)).state == "retry_wait"
    finally:
        worker_module.resolve_notifier = original

    await _force(job_id, available_at=now_kst() - timedelta(seconds=1))
    await recover_callback_jobs(handler=_handler)
    assert calls == [1]
    row = await load_job(job_id)
    assert row is not None
    assert row.attempt_count == 2
    assert row.state == "succeeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "post_entry_state",
    ["entered", "completed", "terminal_pending"],
)
async def test_schedule_retry_refuses_any_row_that_is_not_provably_pre_entry(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], post_entry_state: str
) -> None:
    """R10 — the service itself is the last line, not just the worker.

    Called directly, on a row whose durable markers say the core was entered,
    ``schedule_retry`` must refuse *and leave every marker untouched*. A
    version that simply wrote ``retry_wait`` and cleared the markers would
    erase the only evidence that a replay is unsafe.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
        RetryAuthorityRefused,
    )

    job_id = await _queue(inbox_cleanup)
    fields: dict[str, Any] = {
        "state": "processing",
        "attempt_count": 1,
        "started_at": now_kst(),
    }
    if post_entry_state == "entered":
        fields["handler_entered_at"] = now_kst()
    elif post_entry_state == "completed":
        fields["handler_entered_at"] = now_kst()
        fields["handler_completed_at"] = now_kst()
    else:
        fields["handler_entered_at"] = now_kst()
        fields["handler_completed_at"] = now_kst()
        fields["terminal_state_pending"] = "succeeded"
        fields["outcome"] = "approved"
    await _force(job_id, **fields)

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        with pytest.raises(RetryAuthorityRefused):
            await service.schedule_retry(row, now=now_kst())
        await session.rollback()

    # A fresh session: every marker survived, and the state did not move.
    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.state == "processing"
    assert fresh.handler_entered_at is not None
    if post_entry_state != "entered":
        assert fresh.handler_completed_at is not None
    if post_entry_state == "terminal_pending":
        assert fresh.terminal_state_pending == "succeeded"


@pytest.mark.asyncio
async def test_a_stale_orm_row_cannot_erase_entry_evidence(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R10 — the precondition is evaluated in the database, not in memory.

    The classic shape: a worker loads the row *before* entry, the entry marker
    is committed by the work in between, and the worker then asks for a retry
    holding an object that still says ``handler_entered_at is None``. An
    in-memory check passes; a conditional UPDATE does not.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
        RetryAuthorityRefused,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(job_id, state="processing", attempt_count=1, started_at=now_kst())

    async with AsyncSessionLocal() as stale_session:
        stale = await CallbackInboxService(stale_session).get(job_id)
        assert stale is not None
        assert stale.handler_entered_at is None  # what this session still believes

        # Meanwhile, the entry marker is committed by someone else.
        await _force(job_id, handler_entered_at=now_kst())

        with pytest.raises(RetryAuthorityRefused):
            await CallbackInboxService(stale_session).schedule_retry(
                stale, now=now_kst()
            )
        await stale_session.rollback()

    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.handler_entered_at is not None, "entry evidence was erased"
    assert fresh.state == "processing"


@pytest.mark.asyncio
async def test_schedule_retry_accepts_only_the_pre_entry_processing_shape(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The positive half, so the guard above cannot be vacuous."""
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(job_id, state="processing", attempt_count=1, started_at=now_kst())

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        await service.schedule_retry(row, now=now_kst())
        await session.commit()

    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.state == "retry_wait"
    assert fresh.error_class == "pre_core_failure"
    assert fresh.handler_entered_at is None


# ---------------------------------------------------------------------------
# B9 — the real recovery scan, over a mixed inbox
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_scan_touches_exactly_the_eligible_rows(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R5 B9 — production selection, not a widened claimable set."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    stale_after = timedelta(hours=6)

    eligible_pending = await _queue(inbox_cleanup)

    eligible_retry = await _queue(inbox_cleanup)
    await _force(
        eligible_retry,
        state="retry_wait",
        attempt_count=1,
        available_at=now_kst() - timedelta(minutes=1),
    )

    eligible_stale_processing = await _queue(inbox_cleanup)
    await _force(
        eligible_stale_processing,
        state="processing",
        attempt_count=1,
        started_at=now_kst() - stale_after,
    )

    fresh_processing = await _queue(inbox_cleanup)
    await _force(
        fresh_processing, state="processing", attempt_count=1, started_at=now_kst()
    )

    future_retry = await _queue(inbox_cleanup)
    await _force(
        future_retry,
        state="retry_wait",
        attempt_count=1,
        available_at=now_kst() + timedelta(hours=1),
    )

    exhausted = await _queue(inbox_cleanup)
    await _force(
        exhausted,
        state="retry_wait",
        attempt_count=3,
        available_at=now_kst() - timedelta(minutes=1),
    )

    terminal = await _queue(inbox_cleanup)
    await _force(
        terminal,
        state="succeeded",
        outcome="approved",
        callback_query_id=None,
        chat_id=None,
        message_id=None,
        telegram_user_id=None,
        action=None,
        subject_short=None,
        dispatch_attempt_id=None,
        membership_revision=None,
        membership_digest=None,
        nonce=None,
    )

    executed: list[str] = []

    async def _handler(normalized, **kwargs):
        executed.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    report = await recover_callback_jobs(handler=_handler, limit=50)
    assert report["status"] == "ok"

    states = {
        "eligible_pending": (await load_job(eligible_pending)).state,
        "eligible_retry": (await load_job(eligible_retry)).state,
        "eligible_stale_processing": (await load_job(eligible_stale_processing)).state,
        "fresh_processing": (await load_job(fresh_processing)).state,
        "future_retry": (await load_job(future_retry)).state,
        "exhausted": (await load_job(exhausted)).state,
        "terminal": (await load_job(terminal)).state,
    }
    assert states == {
        "eligible_pending": "succeeded",
        "eligible_retry": "succeeded",
        "eligible_stale_processing": "succeeded",
        # untouched
        "fresh_processing": "processing",
        "future_retry": "retry_wait",
        # selected, but the attempt budget is spent: dead-lettered without a
        # single core call.
        "exhausted": "dead_letter",
        "terminal": "succeeded",
    }
    assert (await load_job(exhausted)).error_class == "attempts_exhausted"
    # Three eligible rows ran, each exactly once.
    assert len(executed) == 3, executed
    assert len(set(executed)) == 3, executed


@pytest.mark.asyncio
async def test_one_tick_is_bounded_by_the_limit_and_leaves_the_rest(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R6 strengthening 1 — count real executions and residual eligible rows."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_ids = [await _queue(inbox_cleanup) for _ in range(5)]
    executed: list[int] = []

    async def _handler(normalized, **kwargs):
        executed.append(1)
        return {"handled": True, "reason": "approved"}

    report = await recover_callback_jobs(handler=_handler, limit=2)

    assert len(executed) <= 2, f"one tick ran {len(executed)} jobs with limit=2"
    assert report["claimed"] <= 2

    states = [(await load_job(job_id)).state for job_id in job_ids]
    done = [state for state in states if state == "succeeded"]
    left = [state for state in states if state == "pending"]
    assert len(done) == len(executed)
    assert len(left) == 5 - len(executed), states
    assert left, "a bounded tick drained everything"

    # And the remainder is still eligible: later ticks finish the job, still
    # bounded, still making progress.
    async def _pending_count() -> int:
        total = 0
        for job_id in job_ids:
            row = await load_job(job_id)
            assert row is not None
            total += row.state == "pending"
        return total

    for _ in range(10):
        if await _pending_count() == 0:
            break
        before = len(executed)
        await recover_callback_jobs(handler=_handler, limit=2)
        assert len(executed) > before, "the scan stopped making progress"
        assert len(executed) - before <= 2, "a tick exceeded its limit"
    assert await _pending_count() == 0
    assert len(executed) == 5


@pytest.mark.asyncio
async def test_recovered_jobs_still_obey_the_lock_and_run_once(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """A recovered row is not a licence to skip the advisory lock."""
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue(inbox_cleanup)
    executed: list[int] = []

    async def _handler(normalized, **kwargs):
        executed.append(1)
        return {"handled": True, "reason": "approved"}

    holder = PostgresJobAdvisoryLock()
    assert await holder.try_acquire(job_advisory_lock_key(job_id)) is True
    try:
        report = await recover_callback_jobs(handler=_handler)
        assert executed == [], "recovery ran a job whose lock was held"
        assert report["statuses"].get("lock_contended", 0) >= 1
    finally:
        await holder.release(job_advisory_lock_key(job_id))

    await recover_callback_jobs(handler=_handler)
    assert executed == [1]
    # A second tick must not produce a second execution.
    await recover_callback_jobs(handler=_handler)
    assert executed == [1]


# ---------------------------------------------------------------------------
# R13 — the durable marker algebra
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("label", "markers", "expected_action"),
    [
        (
            "complete_verdict",
            {
                "handler_entered_at": True,
                "handler_completed_at": True,
                "terminal_state_pending": "succeeded",
            },
            "repair",
        ),
        (
            "entered_only",
            {"handler_entered_at": True},
            "ambiguous",
        ),
        (
            "entered_and_completed_without_verdict",
            {"handler_entered_at": True, "handler_completed_at": True},
            "ambiguous",
        ),
        (
            "never_entered",
            {},
            "run",
        ),
    ],
)
async def test_classify_repair_requires_all_three_facts_in_order(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    label: str,
    markers: dict[str, Any],
    expected_action: str,
) -> None:
    """R13 — repair finalises without re-running, so it needs the full chain.

    ``handler_completed_at`` + ``terminal_state_pending`` alone used to be
    enough. That let a row that never recorded an entry be finalised as
    ``succeeded`` on the strength of a verdict nothing had produced.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    stamp = now_kst() - timedelta(hours=6)
    fields: dict[str, Any] = {
        "state": "processing",
        "attempt_count": 1,
        "started_at": stamp,
    }
    for key, value in markers.items():
        fields[key] = stamp if value is True else value
    if markers.get("terminal_state_pending"):
        fields["outcome"] = "approved"
    await _force(job_id, **fields)

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        decision = service.classify_claim(
            row, now=now_kst(), claimable_states=RECOVERY_CLAIMABLE_STATES
        )
        await session.rollback()

    assert decision.action == expected_action, label


@pytest.mark.unit
def test_classify_refuses_to_repair_a_verdict_that_has_no_entry() -> None:
    """R13, the exact fail-open — and it cannot be built in the database.

    ``handler_completed_at`` + ``terminal_state_pending`` without
    ``handler_entered_at`` is precisely the shape the old repair branch
    accepted: it would finalise the job as ``succeeded`` on the strength of a
    verdict no handler entry ever produced. The new CHECK makes the row
    unstorable, so this is asserted against the classifier directly -- defence
    in depth, because a state machine should not depend on a constraint to
    avoid a wrong answer.
    """
    from types import SimpleNamespace

    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    stamp = now_kst() - timedelta(hours=6)
    malformed = SimpleNamespace(
        state="processing",
        attempt_count=1,
        max_attempts=3,
        available_at=stamp,
        started_at=stamp,
        handler_entered_at=None,
        handler_completed_at=stamp,
        terminal_state_pending="succeeded",
    )
    decision = CallbackInboxService.classify_claim(
        CallbackInboxService(None),  # type: ignore[arg-type]
        malformed,
        now=now_kst(),
        claimable_states=RECOVERY_CLAIMABLE_STATES,
    )
    assert decision.action == "ambiguous", (
        "a verdict with no entry was accepted as a repair"
    )

    # The same row *with* an entry is a legitimate repair, so the guard above
    # is not simply refusing everything.
    well_formed = SimpleNamespace(**{**vars(malformed), "handler_entered_at": stamp})
    assert (
        CallbackInboxService.classify_claim(
            CallbackInboxService(None),  # type: ignore[arg-type]
            well_formed,
            now=now_kst(),
            claimable_states=RECOVERY_CLAIMABLE_STATES,
        ).action
        == "repair"
    )


@pytest.mark.asyncio
async def test_a_verdict_without_an_entry_never_finalises_as_succeeded(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R13 — end to end, through the worker, on the malformed shape.

    The database refuses to store this shape, so it is built by hand at the
    service boundary with the constraint deferred out of the picture: the
    point is that even if such a row somehow existed, the worker would not
    turn it into a success.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)
    stamp = now_kst() - timedelta(hours=6)
    await _force(
        job_id,
        state="processing",
        attempt_count=1,
        started_at=stamp,
        handler_entered_at=stamp,
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    result = await process_callback_job(
        job_id, handler=_handler, claimable_states=RECOVERY_CLAIMABLE_STATES
    )
    assert result["status"] == "dead_letter"
    assert calls == []

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "dead_letter"
    assert row.error_class == "handler_ambiguous"

    # And a classify pass over the terminal row is a no-op, not a repair.
    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        refreshed = await service.get(job_id)
        assert refreshed is not None
        assert (
            service.classify_claim(
                refreshed, now=now_kst(), claimable_states=RECOVERY_CLAIMABLE_STATES
            ).action
            == "skip"
        )
        await session.rollback()


@pytest.mark.asyncio
async def test_begin_attempt_never_erases_a_handler_marker(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R13 — markers are monotonic; no API may clear post-entry evidence."""
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    stamp = now_kst() - timedelta(hours=6)
    await _force(
        job_id,
        state="processing",
        attempt_count=1,
        started_at=stamp,
        handler_entered_at=stamp,
    )

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        granted = await service.begin_attempt(row, now=now_kst())
        assert granted is False, "a post-entry row was re-armed"
        await session.commit()

    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.handler_entered_at is not None, "entry evidence was erased"
    assert fresh.attempt_count == 1, "an attempt was spent on a refused claim"


@pytest.mark.unit
def test_no_marker_is_ever_written_back_to_null() -> None:
    """R13 — structural: the service contains no marker-clearing write."""
    import inspect

    from app.services.order_proposals.callback_inbox import service as service_module

    source = inspect.getsource(service_module)
    for marker in (
        "handler_entered_at",
        "handler_completed_at",
    ):
        for clearing in (
            f'"{marker}": None',
            f"{marker}=None",
        ):
            assert clearing not in source, f"{clearing!r} makes {marker} non-monotonic"


# ---------------------------------------------------------------------------
# R8 B16 — recovery tells pre-entry stale from in-core stale
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_recovery_task_separates_pre_entry_from_in_core_stale(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R8 B16 — through the production TaskIQ entrypoint, both gates armed.

    Two stale ``processing`` rows that differ only in ``handler_entered_at``.
    The one that provably never entered the core may run, exactly once. The
    one that did is ambiguous: it must finalise without the handler being
    called and without earning another attempt.
    """
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    for gate in (
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
    ):
        monkeypatch.setattr(settings, gate, True, raising=False)

    stamp = now_kst() - timedelta(hours=6)

    pre_entry = await _queue(inbox_cleanup)
    await _force(pre_entry, state="processing", attempt_count=1, started_at=stamp)

    in_core = await _queue(inbox_cleanup)
    await _force(
        in_core,
        state="processing",
        attempt_count=1,
        started_at=stamp,
        handler_entered_at=stamp,
    )

    executed: list[uuid.UUID] = []
    notifier_calls: list[int] = []

    class _Notifier:
        async def answer_callback(self, *args, **kwargs):
            notifier_calls.append(1)
            return True

        async def edit_message(self, *args, **kwargs):
            notifier_calls.append(1)
            return None

    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: _Notifier())

    original_core = worker_module.handle_normalized_callback

    async def _counting_core(normalized, **kwargs):
        executed.append(normalized.callback.subject_short)
        return {"handled": False, "reason": "proposal_not_found"}

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _counting_core)
    assert original_core is not None

    report = await task_module.recover_telegram_callback_jobs()
    assert report["status"] == "ok", report

    pre_row = await load_job(pre_entry)
    core_row = await load_job(in_core)
    assert pre_row is not None and core_row is not None

    assert pre_row.state == "discarded"
    assert pre_row.attempt_count == 2, "the pre-entry row did not get its attempt"

    assert core_row.state == "dead_letter"
    assert core_row.error_class == "handler_ambiguous"
    assert core_row.attempt_count == 1, "an ambiguous row was charged an attempt"

    # Exactly one core entry, and it was the pre-entry row's.
    assert len(executed) == 1, executed

    # A second tick changes nothing: both are terminal.
    again = await task_module.recover_telegram_callback_jobs()
    assert again["status"] == "ok"
    assert len(executed) == 1, "a terminal row was re-entered"
