"""W5 — the recovery scan: what a lost Redis kick costs, and what it reports.

Acceptance: "with the scheduler enabled in test config, Redis loss is
recovered within two simulated cron ticks."

The scan's output is aggregate-only by construction — counts and one age, no
per-job identifiers beyond the opaque job UUID — because it is the surface an
operator will read most often.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import (
    FakeNotifier,
    load_job,
    make_update,
    proposal_callback_data,
    shape_owned_callback_inbox_row,
)

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


async def _queue_without_kick(
    inbox_cleanup: list[uuid.UUID], *, data: str | None = None
) -> uuid.UUID:
    """Exactly the state a lost Redis kick leaves behind: committed, unqueued."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 960_000 + uuid.uuid4().int % 100_000

    async def _redis_is_down(job_id: uuid.UUID) -> None:
        raise ConnectionError("redis is gone")

    result = await ingest_callback_update(
        make_update(
            data=data or _synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_redis_is_down,
    )
    assert result.accepted is True
    assert result.enqueued is False
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


@pytest.mark.asyncio
async def test_a_lost_kick_is_recovered_within_two_cron_ticks(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    from .conftest import seed_proposal

    group = await seed_proposal(db_session, nonce="recover1234", symbol="RCVKR")
    job_id = await _queue_without_kick(
        inbox_cleanup, data=proposal_callback_data(group)
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    tick_one = await recover_callback_jobs(handler=_handler)
    assert tick_one["status"] == "ok"

    row = await load_job(job_id)
    assert row is not None
    if row.state == "pending":
        # A second tick is the acceptance bar; anything more is a regression.
        await recover_callback_jobs(handler=_handler)
        row = await load_job(job_id)

    assert row is not None
    assert row.state == "succeeded", "not recovered within two cron ticks"
    assert calls == [1]


@pytest.mark.asyncio
async def test_recovery_reports_aggregate_backlog_only(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    await _queue_without_kick(inbox_cleanup)

    async def _handler(normalized, **kwargs):
        return {"handled": False, "reason": "proposal_not_found"}

    report = await recover_callback_jobs(handler=_handler)

    assert report["status"] == "ok"
    # ``scanned`` joined the contract in R29: without it there is no way to
    # tell from outside how much a tick looked at, so the scan cap could not
    # be observed. Still aggregate-only -- a count, not an identifier.
    assert set(report) == {"status", "scanned", "claimed", "statuses", "backlog"}
    assert isinstance(report["claimed"], int)
    # ``scanned`` is a plain count inside the exact cap for this tick's
    # execution limit -- asserting the bound is the whole reason it is
    # reported, so it is asserted here rather than merely typed.
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_SCAN_LIMIT,
        recovery_scan_cap,
    )

    assert isinstance(report["scanned"], int)
    assert not isinstance(report["scanned"], bool)
    assert 0 <= report["scanned"] <= recovery_scan_cap(RECOVERY_SCAN_LIMIT)
    assert report["scanned"] >= report["claimed"]
    # Counts by state and one age. No identifiers, no chat, no nonce.
    assert set(report["backlog"]) == {
        "pending",
        "processing",
        "retry_wait",
        "dead_letter",
        "oldest_pending_age_seconds",
    }
    for key, value in report["backlog"].items():
        assert isinstance(value, int | float | type(None)), key
    for status, count in report["statuses"].items():
        assert isinstance(status, str) and isinstance(count, int)


@pytest.mark.asyncio
async def test_recovery_bounds_how_much_it_claims_per_tick(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    for _ in range(3):
        await _queue_without_kick(inbox_cleanup)

    async def _handler(normalized, **kwargs):
        return {"handled": False, "reason": "proposal_not_found"}

    report = await recover_callback_jobs(handler=_handler, limit=2)
    assert report["claimed"] <= 2


@pytest.mark.asyncio
async def test_recovery_leaves_a_not_yet_due_retry_alone(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue_without_kick(inbox_cleanup)
    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(
            session,
            job_id,
            state="retry_wait",
            attempt_count=1,
            error_class="pre_core_failure",
            available_at=now_kst() + timedelta(hours=1),
        )
        await session.commit()

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True}

    await recover_callback_jobs(handler=_handler)
    assert calls == []
    row = await load_job(job_id)
    assert row is not None
    assert row.state == "retry_wait"


@pytest.mark.asyncio
async def test_recovery_ignores_a_processing_row_that_is_not_yet_stale(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The staleness window is a scan filter; the lock is the authority."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue_without_kick(inbox_cleanup)
    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(
            session, job_id, state="processing", attempt_count=1, started_at=now_kst()
        )
        await session.commit()

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True}

    await recover_callback_jobs(handler=_handler)
    assert calls == []
    row = await load_job(job_id)
    assert row is not None
    assert row.state == "processing"


@pytest.mark.asyncio
async def test_recovery_never_touches_a_terminal_row(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    from .conftest import seed_proposal

    group = await seed_proposal(db_session, nonce="terminal123", symbol="TRMKR")
    job_id = await _queue_without_kick(
        inbox_cleanup, data=proposal_callback_data(group)
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    assert (await process_callback_job(job_id, handler=_handler))[
        "status"
    ] == "succeeded"
    assert calls == [1]

    await recover_callback_jobs(handler=_handler)
    await recover_callback_jobs(handler=_handler)
    assert calls == [1], "recovery re-ran a terminal job"

    notifier = FakeNotifier()
    assert notifier.external_calls == 0
