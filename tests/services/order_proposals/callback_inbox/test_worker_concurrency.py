"""W5 — exclusive claim, crash release and reclaim, against real PostgreSQL.

RED-before-fix items 8, 9 and 10.

The authority is the PostgreSQL session advisory lock held on the worker's
own dedicated connection — never a lease timestamp. Every test here proves a
handler-invocation *count*, because "the handler ran twice" is the failure
this whole design exists to prevent.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta

import pytest

from app.core.timezone import now_kst

from .conftest import CHAT_ID, load_job, make_update

pytestmark = pytest.mark.integration


async def _queue_job(inbox_cleanup: list[uuid.UUID], *, data: str) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 800_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(data=data, update_id=update_id, callback_id=f"cbq-{update_id}"),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


def _valid_data() -> str:
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    proposal_id = uuid.uuid4()
    nonce = "nonce123456"
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


@pytest.mark.asyncio
async def test_two_concurrent_tasks_for_one_job_invoke_the_handler_once(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 8 — a duplicate Redis message must not double-run a job."""
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    job_id = await _queue_job(inbox_cleanup, data=_valid_data())
    entered = asyncio.Event()
    calls: list[uuid.UUID] = []

    async def _handler(normalized, **kwargs):
        calls.append(job_id)
        entered.set()
        await asyncio.sleep(0.4)
        return {"handled": True, "reason": "approved"}

    first = asyncio.create_task(process_callback_job(job_id, handler=_handler))
    await asyncio.wait_for(entered.wait(), timeout=10)
    second = await process_callback_job(job_id, handler=_handler)
    first_result = await first

    assert len(calls) == 1
    assert first_result["status"] == "succeeded"
    assert second["status"] == "lock_contended"
    assert second["job_id"] == str(job_id)
    assert set(second) == {"status", "job_id"}


@pytest.mark.asyncio
async def test_recovery_will_not_touch_a_job_whose_lock_is_alive(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 9 — a live lock beats any staleness heuristic."""
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    job_id = await _queue_job(inbox_cleanup, data=_valid_data())

    # Park the row in `processing` and back-date it far past any staleness
    # window, so only the live lock can hold recovery off.
    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        await service.force_state_for_test(
            job_id,
            state="processing",
            started_at=now_kst() - timedelta(hours=6),
            attempt_count=1,
        )
        await session.commit()

    holder = PostgresJobAdvisoryLock()
    assert await holder.try_acquire(job_advisory_lock_key(job_id)) is True
    try:
        calls: list[int] = []

        async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
            calls.append(1)
            return {"handled": True, "reason": "approved"}

        result = await process_callback_job(
            job_id, handler=_handler, claimable_states=RECOVERY_CLAIMABLE_STATES
        )
        assert result["status"] == "lock_contended"
        assert calls == []
        row = await load_job(job_id)
        assert row is not None
        assert row.state == "processing"
        assert row.attempt_count == 1
    finally:
        await holder.release(job_advisory_lock_key(job_id))


@pytest.mark.asyncio
async def test_a_crashed_worker_releases_the_lock_and_recovery_reclaims(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 10 — process death is the only thing that ends a claim early."""
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    job_id = await _queue_job(inbox_cleanup, data=_valid_data())
    key = job_advisory_lock_key(job_id)

    # A worker that claimed, committed `processing`, then died.
    crashed = PostgresJobAdvisoryLock()
    assert await crashed.try_acquire(key) is True
    async with AsyncSessionLocal() as session:
        await CallbackInboxService(session).force_state_for_test(
            job_id,
            state="processing",
            started_at=now_kst() - timedelta(hours=6),
            attempt_count=1,
        )
        await session.commit()
    await crashed.simulate_process_death()

    calls: list[int] = []

    async def _handler(normalized, **kwargs):
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    result = await process_callback_job(
        job_id, handler=_handler, claimable_states=RECOVERY_CLAIMABLE_STATES
    )
    assert result["status"] == "succeeded"
    assert calls == [1]

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    # The crashed attempt still counts toward the poison budget.
    assert row.attempt_count == 2


@pytest.mark.asyncio
async def test_the_per_job_worker_never_steals_a_processing_row(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Only recovery may reclaim `processing`, and only behind the lock."""
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    job_id = await _queue_job(inbox_cleanup, data=_valid_data())
    async with AsyncSessionLocal() as session:
        await CallbackInboxService(session).force_state_for_test(
            job_id,
            state="processing",
            started_at=now_kst() - timedelta(hours=6),
            attempt_count=1,
        )
        await session.commit()

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    result = await process_callback_job(job_id, handler=_handler)
    assert result["status"] == "not_claimable"
    assert calls == []


@pytest.mark.asyncio
async def test_the_attempt_is_committed_before_the_handler_runs(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Poison counting depends on this: a crash mid-handler must be visible."""
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    job_id = await _queue_job(inbox_cleanup, data=_valid_data())
    observed: list[tuple[str, int]] = []

    async def _handler(normalized, **kwargs):
        row = await load_job(job_id)
        assert row is not None
        observed.append((row.state, row.attempt_count))
        return {"handled": True, "reason": "approved"}

    await process_callback_job(job_id, handler=_handler)
    assert observed == [("processing", 1)]


@pytest.mark.asyncio
async def test_an_unknown_job_id_is_reported_without_running_anything(
    _bootstrap_test_schema,
) -> None:
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True}

    result = await process_callback_job(uuid.uuid4(), handler=_handler)
    assert result["status"] == "not_found"
    assert calls == []


@pytest.mark.asyncio
async def test_a_revoked_chat_is_discarded_at_the_worker_without_running(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingress allowlisting is not enough: the worker re-checks current settings."""
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    job_id = await _queue_job(inbox_cleanup, data=_valid_data())
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        str(CHAT_ID + 1),
        raising=False,
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True}

    result = await process_callback_job(job_id, handler=_handler)
    assert result["status"] == "discarded"
    assert calls == []

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "discarded"
    assert row.error_class == "chat_revoked"
    assert row.chat_id is None
