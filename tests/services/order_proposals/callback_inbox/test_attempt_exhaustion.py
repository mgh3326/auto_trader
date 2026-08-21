"""W5 — the last allowed attempt terminates, it does not park.

Adversarial review R22. `_run_locked` called `schedule_retry` on every caught
`PreCoreFailure` without asking whether any attempts remained, so the third
and final attempt left the job in `retry_wait` for the full 300-second
backoff. `classify_claim` then made it worse by checking "not due" before
"exhausted", so the row was invisible until the backoff elapsed and a fourth
claim arrived.

The runbook's contract is that three re-runnable attempts spent means
`dead_letter` / `attempts_exhausted` **with the authority scrubbed** -- and it
should mean that at the moment the third one fails, not five minutes later.
Until then the row still carries the chat id, the user id and a live nonce.

Both crash shapes are covered: the caught `PreCoreFailure` path, and the
`BaseException` process-death path that reclaims through recovery.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import (
    load_job,
    make_update,
    without_the_retry_budget_check,
)

pytestmark = pytest.mark.integration

#: Every authority/PII column a terminal row must have dropped.
AUTHORITY_FIELDS = (
    "callback_query_id",
    "chat_id",
    "message_id",
    "telegram_user_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)


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

    update_id = 650_000 + uuid.uuid4().int % 100_000

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


async def _make_due(job_id: uuid.UUID) -> None:
    """Bring a parked retry forward, exactly as its backoff elapsing would."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    async with AsyncSessionLocal() as session:
        await CallbackInboxService(session).force_state_for_test(
            job_id, available_at=now_kst() - timedelta(seconds=1)
        )
        await session.commit()


async def _raw_row(job_id: uuid.UUID) -> dict[str, Any]:
    """Read the row through raw SQL, not the ORM identity map."""
    async with AsyncSessionLocal() as session:
        row = (
            (
                await session.execute(
                    sa.text(
                        "SELECT * FROM review.telegram_callback_inbox "
                        "WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


@pytest.mark.asyncio
async def test_the_third_pre_core_failure_dead_letters_immediately(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R22 — attempts 1 and 2 park; attempt 3 terminates on the spot."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.contracts import MAX_ATTEMPTS
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    assert MAX_ATTEMPTS == 3

    job_id = await _queue(inbox_cleanup)
    handler_calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        handler_calls.append(1)
        return {"handled": True, "reason": "approved"}

    def _pre_core_boom(*args: Any, **kwargs: Any):
        raise RuntimeError("notifier unavailable")

    original = worker_module.resolve_notifier
    worker_module.resolve_notifier = _pre_core_boom
    try:
        # -- attempts 1 and 2: bounded retry ------------------------------
        for attempt in (1, 2):
            result = await process_callback_job(job_id, handler=_handler)
            assert result["status"] == "retry_scheduled", (attempt, result)
            row = await load_job(job_id)
            assert row is not None
            assert row.state == "retry_wait", attempt
            assert row.attempt_count == attempt
            assert row.error_class == "pre_core_failure"
            # Still runnable, so the authority is still there.
            assert row.nonce is not None and row.chat_id is not None
            await _make_due(job_id)

        # -- attempt 3: the last allowed one. It must not park. -----------
        final = await process_callback_job(job_id, handler=_handler)
    finally:
        worker_module.resolve_notifier = original

    assert final["status"] == "dead_letter", final
    assert handler_calls == [], "a pre-core failure reached the handler"

    raw = await _raw_row(job_id)
    assert raw["state"] == "dead_letter", raw["state"]
    assert raw["error_class"] == "attempts_exhausted", raw["error_class"]
    assert raw["attempt_count"] == 3
    assert raw["finished_at"] is not None

    # The whole point: the authority is gone *now*, not after a backoff.
    for field in AUTHORITY_FIELDS:
        assert raw[field] is None, field


@pytest.mark.asyncio
async def test_the_terminal_state_needs_no_fourth_claim_and_no_waiting(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R22 — no elapsed backoff and no extra claim may be required."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the handler was reached")

    def _pre_core_boom(*args: Any, **kwargs: Any):
        raise RuntimeError("notifier unavailable")

    original = worker_module.resolve_notifier
    worker_module.resolve_notifier = _pre_core_boom
    try:
        for _ in range(2):
            await process_callback_job(job_id, handler=_handler)
            await _make_due(job_id)
        await process_callback_job(job_id, handler=_handler)
    finally:
        worker_module.resolve_notifier = original

    # Immediately after the third attempt -- nothing advanced the clock and
    # nothing claimed the row again.
    raw = await _raw_row(job_id)
    assert raw["state"] == "dead_letter"
    assert raw["error_class"] == "attempts_exhausted"

    # And a recovery sweep is a no-op on it.
    report = await recover_callback_jobs(handler=_handler)
    assert report["status"] == "ok"
    assert (await _raw_row(job_id))["state"] == "dead_letter"


@pytest.mark.asyncio
async def test_an_exhausted_row_is_classified_exhausted_not_merely_not_due(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R22 — exhaustion must outrank the backoff window in the classifier.

    A row that somehow reached `retry_wait` with its budget spent (an older
    binary, a hand edit) must be recognised as finished rather than hidden
    behind a not-yet-due window until the backoff elapses.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    # R25: the database now refuses this shape outright, so the only way to
    # hold a classifier-level regression test on it is to build the row the
    # way an older binary would have.
    async with without_the_retry_budget_check():
        async with AsyncSessionLocal() as session:
            service = CallbackInboxService(session)
            await service.force_state_for_test(
                job_id,
                state="retry_wait",
                attempt_count=3,
                error_class="pre_core_failure",
                available_at=now_kst() + timedelta(hours=1),
            )
            await session.commit()

        async with AsyncSessionLocal() as session:
            service = CallbackInboxService(session)
            row = await service.get(job_id)
            assert row is not None
            decision = service.classify_claim(
                row, now=now_kst(), claimable_states=RECOVERY_CLAIMABLE_STATES
            )
            await session.rollback()

    assert decision.action == "exhausted", decision


@pytest.mark.asyncio
async def test_repeated_process_death_still_dead_letters_at_the_budget(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R22 — the other crash shape: BaseException, reclaimed by recovery.

    A pre-entry `BaseException` unwinds without the worker recording
    anything, so the row stays `processing` and recovery reclaims it. That
    path must land on the same budget and the same terminal scrub.
    """
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.contracts import (
        MAX_ATTEMPTS,
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)

    class _ProcessDied(BaseException):
        """Not an ``Exception``: nothing can swallow this."""

    def _die(*args: Any, **kwargs: Any):
        raise _ProcessDied

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("the handler was reached")

    original = worker_module.resolve_notifier
    worker_module.resolve_notifier = _die
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            with pytest.raises(_ProcessDied):
                await process_callback_job(
                    job_id, handler=_handler, claimable_states=RECOVERY_CLAIMABLE_STATES
                )
            row = await load_job(job_id)
            assert row is not None
            assert row.attempt_count == attempt
            assert row.state == "processing"
            assert row.handler_entered_at is None
            async with AsyncSessionLocal() as session:
                await CallbackInboxService(session).force_state_for_test(
                    job_id, started_at=now_kst() - timedelta(hours=6)
                )
                await session.commit()

        final = await process_callback_job(
            job_id, handler=_handler, claimable_states=RECOVERY_CLAIMABLE_STATES
        )
    finally:
        worker_module.resolve_notifier = original

    assert final["status"] == "dead_letter"
    raw = await _raw_row(job_id)
    assert raw["state"] == "dead_letter"
    assert raw["error_class"] == "attempts_exhausted"
    for field in AUTHORITY_FIELDS:
        assert raw[field] is None, field
