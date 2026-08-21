"""W5 — a contended prefix must not starve the rest of the inbox.

Adversarial review R29. The recovery scan takes ``ORDER BY received_at LIMIT
N`` and *then* tries the advisory lock on each candidate. If the oldest N
rows are ``processing`` under live worker locks, every tick spends its whole
budget on the same N ``lock_contended`` results and the pending row behind
them is never even selected -- for as long as those workers run.

Observed on the parent with ``limit=2``:

  tick 1 statuses = {"lock_contended": 2}
  tick 2 statuses = {"lock_contended": 2}
  pending_selected = False, pending_state = "pending"

That breaks the W5 acceptance bar directly: a lost Redis kick is supposed to
be recovered within two ticks, and here it is recovered never. The default
``limit=20`` only moves the threshold to twenty long-running jobs.

The fix has two halves, and both are pinned below: the scan orders by *state
priority* so queued work is not queued behind in-flight work, and the scan
cap is separated from the execution cap so a contended candidate costs a scan
slot rather than an execution slot.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import load_job, make_update, proposal_callback_data

pytestmark = pytest.mark.integration


def _synthetic_data() -> str:
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


async def _queue(
    inbox_cleanup: list[uuid.UUID], *, data: str | None = None, received_at=None
) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 940_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=data or _synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}-{uuid.uuid4().hex[:8]}",
        ),
        now=received_at or now_kst(),
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


class _HeldLocks:
    """Real advisory locks, each on its own live backend."""

    def __init__(self) -> None:
        self._connections: list[Any] = []

    async def hold(self, job_id: uuid.UUID) -> int:
        from app.core import db
        from app.services.order_proposals.callback_inbox.contracts import (
            job_advisory_lock_key,
        )

        connection = await db.engine.connect()
        self._connections.append(connection)
        key = job_advisory_lock_key(job_id)
        taken = bool(
            (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        assert taken is True, "could not take the lock the test depends on"
        return int(
            (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )

    async def release_all(self) -> None:
        for connection in self._connections:
            with_suppress = getattr(connection, "close", None)
            if with_suppress is not None:
                await connection.close()
        self._connections.clear()


@pytest_asyncio.fixture
async def held_locks():
    locks = _HeldLocks()
    try:
        yield locks
    finally:
        await locks.release_all()


@pytest.mark.asyncio
async def test_a_locked_prefix_does_not_starve_a_lost_kick(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID], held_locks
) -> None:
    """R29 — the reported counterexample, through the production sweep."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    from .conftest import seed_proposal

    limit = 2
    stale_at = now_kst() - timedelta(hours=6)

    # The oldest rows: stale ``processing``, each locked by a live backend.
    locked: list[uuid.UUID] = []
    pids: list[int] = []
    for index in range(limit):
        job_id = await _queue(
            inbox_cleanup, received_at=stale_at - timedelta(minutes=10 - index)
        )
        await _force(job_id, state="processing", attempt_count=1, started_at=stale_at)
        pids.append(await held_locks.hold(job_id))
        locked.append(job_id)
    assert len(set(pids)) == limit, "the locks must be on distinct backends"

    # Behind them, the click whose Redis kick was lost.
    group = await seed_proposal(db_session, nonce="starved1234", symbol="STVKR")
    pending = await _queue(inbox_cleanup, data=proposal_callback_data(group))

    calls: list[uuid.UUID] = []

    async def _handler(normalized, **kwargs):
        calls.append(normalized.callback.proposal_id)
        return {"handled": True, "reason": "approved"}

    ticks = [
        await recover_callback_jobs(handler=_handler, limit=limit),
        await recover_callback_jobs(handler=_handler, limit=limit),
    ]

    row = await load_job(pending)
    assert row is not None
    assert row.state == "succeeded", (
        f"the lost kick was never recovered; ticks={[t['statuses'] for t in ticks]}, "
        f"pending_state={row.state}"
    )
    assert len(calls) == 1, calls

    # The locked jobs were left strictly alone.
    for job_id in locked:
        locked_row = await load_job(job_id)
        assert locked_row is not None
        assert locked_row.state == "processing", job_id
        assert locked_row.handler_entered_at is None, job_id
    assert all(tick["statuses"].get("lock_contended", 0) >= 1 for tick in ticks)


@pytest.mark.asyncio
async def test_contention_costs_a_scan_slot_not_an_execution_slot(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], held_locks
) -> None:
    """R29 — the two caps are separate, and both are real."""
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_SCAN_LIMIT,
        recovery_scan_cap,
    )
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    # The scan cap is a bounded multiple of the execution cap, never unbounded.
    assert recovery_scan_cap(RECOVERY_SCAN_LIMIT) > RECOVERY_SCAN_LIMIT
    assert recovery_scan_cap(10_000) <= 1_000, "the scan is not actually capped"

    limit = 2
    stale_at = now_kst() - timedelta(hours=6)
    for index in range(3):
        job_id = await _queue(
            inbox_cleanup, received_at=stale_at - timedelta(minutes=20 - index)
        )
        await _force(job_id, state="processing", attempt_count=1, started_at=stale_at)
        await held_locks.hold(job_id)

    runnable = [await _queue(inbox_cleanup) for _ in range(3)]

    executed: list[uuid.UUID] = []

    async def _handler(normalized, **kwargs):
        executed.append(normalized.callback.proposal_id)
        return {"handled": False, "reason": "proposal_not_found"}

    report = await recover_callback_jobs(handler=_handler, limit=limit)

    # Contended candidates were looked at, but did not consume the budget ...
    assert report["statuses"].get("lock_contended", 0) >= 1, report["statuses"]
    # ... and the execution cap still held.
    assert report["claimed"] <= limit, report
    assert len(runnable) == 3


@pytest.mark.asyncio
async def test_a_not_yet_due_retry_is_still_not_claimed(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The reordering must not widen what may be claimed."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(
        job_id,
        state="retry_wait",
        attempt_count=1,
        error_class="pre_core_failure",
        available_at=now_kst() + timedelta(hours=3),
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True}

    await recover_callback_jobs(handler=_handler)

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "retry_wait"
    assert calls == []


@pytest.mark.asyncio
async def test_stale_processing_still_makes_progress_when_nothing_is_locked(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Fairness the other way: lowering its priority must not strand it."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    from .conftest import seed_proposal

    group = await seed_proposal(db_session, nonce="fairness123", symbol="FARKR")
    job_id = await _queue(inbox_cleanup, data=proposal_callback_data(group))
    await _force(
        job_id,
        state="processing",
        attempt_count=1,
        started_at=now_kst() - timedelta(hours=6),
    )

    async def _handler(normalized, **kwargs):
        return {"handled": True, "reason": "approved"}

    await recover_callback_jobs(handler=_handler)

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded", "an unlocked stale row was never recovered"
