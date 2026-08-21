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
        calls.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    ticks = [
        await recover_callback_jobs(handler=_handler, limit=limit),
        await recover_callback_jobs(handler=_handler, limit=limit),
    ]

    row = await load_job(pending)
    assert row is not None
    assert row.state == "succeeded", (
        f"the lost kick was never recovered; ticks={[t['statuses'] for t in ticks]}, "
        f"pending_state={row.state}, error_class={row.error_class}, "
        f"outcome={row.outcome}"
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
        executed.append(normalized.callback.subject_short)
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


# ---------------------------------------------------------------------------
# fairness has to come from the database, not from this process
# ---------------------------------------------------------------------------
#
# A module-level cursor would pass the two-tick test above and still starve
# the inbox in production: workers restart, and more than one sweeper can be
# running at once. Whatever makes the second tick different from the first has
# to be visible to a process that has never run a sweep before.


@pytest.mark.asyncio
async def test_fairness_survives_a_fresh_recovery_instance(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID], held_locks
) -> None:
    """R29 — reload the module between ticks; the outcome must not change.

    If progress depended on state carried in the recovery module, discarding
    that module would put the sweep back at tick one forever.
    """
    import importlib

    from app.services.order_proposals.callback_inbox import recovery as recovery_module

    from .conftest import seed_proposal

    limit = 2
    stale_at = now_kst() - timedelta(hours=6)
    for index in range(limit):
        job_id = await _queue(
            inbox_cleanup, received_at=stale_at - timedelta(minutes=10 - index)
        )
        await _force(job_id, state="processing", attempt_count=1, started_at=stale_at)
        await held_locks.hold(job_id)

    group = await seed_proposal(db_session, nonce="freshinst12", symbol="FRSKR")
    pending = await _queue(inbox_cleanup, data=proposal_callback_data(group))

    calls: list[Any] = []

    async def _handler(normalized, **kwargs):
        calls.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    for _ in range(2):
        fresh = importlib.reload(recovery_module)
        await fresh.recover_callback_jobs(handler=_handler, limit=limit)

    row = await load_job(pending)
    assert row is not None
    assert row.state == "succeeded", (
        "a process that had never swept before could not reach the pending row"
    )
    assert len(calls) == 1, calls


@pytest.mark.asyncio
async def test_two_concurrent_sweepers_make_progress_without_duplicating(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID], held_locks
) -> None:
    """R29 — two sweepers at once: progress, and still exactly one handler call."""
    import asyncio as _asyncio

    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    from .conftest import seed_proposal

    limit = 2
    stale_at = now_kst() - timedelta(hours=6)
    locked: list[uuid.UUID] = []
    for index in range(limit):
        job_id = await _queue(
            inbox_cleanup, received_at=stale_at - timedelta(minutes=10 - index)
        )
        await _force(job_id, state="processing", attempt_count=1, started_at=stale_at)
        await held_locks.hold(job_id)
        locked.append(job_id)

    group = await seed_proposal(db_session, nonce="concurrent1", symbol="CNCKR")
    pending = await _queue(inbox_cleanup, data=proposal_callback_data(group))

    calls: list[Any] = []

    async def _handler(normalized, **kwargs):
        calls.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    await _asyncio.gather(
        recover_callback_jobs(handler=_handler, limit=limit),
        recover_callback_jobs(handler=_handler, limit=limit),
    )

    row = await load_job(pending)
    assert row is not None
    assert row.state == "succeeded", "neither sweeper reached the pending row"
    assert len(calls) == 1, f"the job ran {len(calls)} times"

    for job_id in locked:
        locked_row = await load_job(job_id)
        assert locked_row is not None
        assert locked_row.state == "processing"
        assert locked_row.handler_entered_at is None


def test_the_sweep_carries_no_process_local_cursor() -> None:
    """R29 — structurally: no module-level mutable state to carry a position."""
    import inspect

    from app.services.order_proposals.callback_inbox import recovery as recovery_module
    from app.services.order_proposals.callback_inbox import (
        repository as repository_module,
    )

    for module in (recovery_module, repository_module):
        for name, value in vars(module).items():
            if name.startswith("__") or inspect.isclass(value):
                continue
            assert not isinstance(value, list | dict | set), (
                f"{module.__name__}.{name} is mutable module state; fairness must "
                f"come from the database, not from this process"
            )


# ---------------------------------------------------------------------------
# fairness runs both ways
# ---------------------------------------------------------------------------
#
# Putting queued work first fixes the reported starvation and creates its
# mirror image: if the queued backlog is larger than the scan cap, the stale
# ``processing`` tier is never reached. Ordering alone cannot satisfy both --
# whichever tier sorts first starves the other whenever it is big enough --
# so each tier needs its own bounded share of the scan and of the execution
# budget, computed from the rows themselves.
#
# This case starves the stale tier under *age* ordering too, by making the
# queued backlog older, so it is a genuine counterexample to both designs.


@pytest.mark.asyncio
async def test_a_queued_backlog_does_not_starve_stale_recovery(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R29 — a stale row must progress even behind an oversized queue."""
    import importlib

    from app.services.order_proposals.callback_inbox import recovery as recovery_module
    from app.services.order_proposals.callback_inbox.contracts import (
        recovery_scan_cap,
    )

    from .conftest import seed_proposal

    limit = 2
    backlog = recovery_scan_cap(limit) + 5
    old = now_kst() - timedelta(hours=12)

    # More queued work than one sweep can even look at, and all of it older
    # than the stale row, so age ordering buries the stale row too.
    for index in range(backlog):
        await _queue(inbox_cleanup, received_at=old + timedelta(seconds=index))

    group = await seed_proposal(db_session, nonce="stalefair12", symbol="SFRKR")
    stale = await _queue(
        inbox_cleanup,
        data=proposal_callback_data(group),
        received_at=now_kst() - timedelta(hours=6),
    )
    await _force(
        stale,
        state="processing",
        attempt_count=1,
        started_at=now_kst() - timedelta(hours=6),
    )

    async def _handler(normalized, **kwargs):
        return {"handled": True, "reason": "approved"}

    reports = []
    for _ in range(2):
        fresh = importlib.reload(recovery_module)
        reports.append(await fresh.recover_callback_jobs(handler=_handler, limit=limit))

    row = await load_job(stale)
    assert row is not None
    assert row.state == "succeeded", (
        "the stale row starved behind the queued backlog; "
        f"statuses={[report['statuses'] for report in reports]}, state={row.state}"
    )

    # The execution cap still holds on every tick.
    for report in reports:
        assert report["claimed"] <= limit, report


@pytest.mark.asyncio
async def test_every_tier_keeps_a_deterministic_age_order(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Sharing the budget must not make selection arbitrary within a tier."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    base = now_kst() - timedelta(hours=9)
    queued = [
        await _queue(inbox_cleanup, received_at=base + timedelta(seconds=index))
        for index in range(4)
    ]

    async with AsyncSessionLocal() as session:
        first = await CallbackInboxService(session).claimable_job_ids(
            now=now_kst(), limit=50
        )
        second = await CallbackInboxService(session).claimable_job_ids(
            now=now_kst(), limit=50
        )
        await session.rollback()

    assert first == second, "the same inbox produced two different orders"
    positions = [first.index(job_id) for job_id in queued]
    assert positions == sorted(positions), "oldest-first was lost inside the tier"


# ---------------------------------------------------------------------------
# the caps and the due filter, asserted positively
# ---------------------------------------------------------------------------
#
# Everything above is about what the sweep must *not* do. A sweep that claimed
# nothing at all would satisfy most of it, so these pin the other side: the
# scan really is capped, a full budget really is spent, and the positive half
# of the due filter really does run.


@pytest.mark.asyncio
async def test_the_report_states_how_much_was_scanned_and_stays_capped(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R29 — ``scanned`` is part of the contract, and it obeys the scan cap."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    limit = 2
    for index in range(limit * 5 + 8):
        await _queue(
            inbox_cleanup, received_at=now_kst() - timedelta(minutes=90 - index)
        )

    async def _handler(normalized, **kwargs):
        return {"handled": True, "reason": "approved"}

    report = await recover_callback_jobs(handler=_handler, limit=limit)

    assert "scanned" in report, sorted(report)

    from app.services.order_proposals.callback_inbox.contracts import (
        recovery_scan_cap,
    )

    assert report["scanned"] <= recovery_scan_cap(limit), report
    assert report["scanned"] >= report["claimed"], report
    assert report["claimed"] <= limit, report


@pytest.mark.asyncio
async def test_a_contended_tick_still_spends_its_whole_budget(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID], held_locks
) -> None:
    """R29 — contention costs scan slots, so the execution budget is *used*.

    The negative version of this ("contention did not consume the budget") is
    satisfied by a sweep that runs nothing whatsoever. This one counts real
    handler invocations, so an unwired helper or a silently empty scan fails.
    """
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    from .conftest import seed_proposal

    limit = 2
    stale_at = now_kst() - timedelta(hours=6)
    for index in range(3):
        job_id = await _queue(
            inbox_cleanup, received_at=stale_at - timedelta(minutes=30 - index)
        )
        await _force(job_id, state="processing", attempt_count=1, started_at=stale_at)
        await held_locks.hold(job_id)

    for index in range(3):
        group = await seed_proposal(
            db_session, nonce=f"budget{index:05d}", symbol=f"BDG{index}R"
        )
        await _queue(inbox_cleanup, data=proposal_callback_data(group))

    calls: list[str] = []

    async def _handler(normalized, **kwargs):
        calls.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    report = await recover_callback_jobs(handler=_handler, limit=limit)

    assert report["statuses"].get("lock_contended", 0) >= 1, report["statuses"]
    assert report["claimed"] == limit, report
    assert len(calls) == limit, calls
    assert len(set(calls)) == limit, calls


@pytest.mark.asyncio
async def test_a_due_retry_is_claimed_and_runs_exactly_once(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R29 — the positive half of the due filter, through the real sweep."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    from .conftest import seed_proposal

    group = await seed_proposal(db_session, nonce="duepositiv", symbol="DUEKR")
    job_id = await _queue(inbox_cleanup, data=proposal_callback_data(group))
    await _force(
        job_id,
        state="retry_wait",
        attempt_count=1,
        error_class="pre_core_failure",
        available_at=now_kst() - timedelta(minutes=1),
    )

    calls: list[str] = []

    async def _handler(normalized, **kwargs):
        calls.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    await recover_callback_jobs(handler=_handler)
    await recover_callback_jobs(handler=_handler)

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded", f"a due retry was never claimed: {row.state}"
    assert len(calls) == 1, f"the due retry ran {len(calls)} times"


# ---------------------------------------------------------------------------
# the cap is applied once, and ties are broken the same way every time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_execution_limit_is_capped_exactly_once(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R29 — one number crosses the boundary, and it is the *execution* limit.

    The sweep derived a scan cap and passed *that* down as ``limit``, and the
    repository then derived a scan cap from it again. Two applications of the
    same multiplier: a run limit of 2 asks for 10 by contract and fetched 50,
    and the shipped default of 20 fetched 500. The early break on the
    execution budget hid it from every test that counted work done.
    """
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    seen: list[int] = []
    original = repo_module.CallbackInboxRepository.claimable_job_ids

    async def _spy(self, **kwargs):
        seen.append(kwargs["limit"])
        return await original(self, **kwargs)

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository, "claimable_job_ids", _spy, raising=True
    )

    await _queue(inbox_cleanup)

    async def _handler(normalized, **kwargs):
        return {"handled": False, "reason": "proposal_not_found"}

    await recover_callback_jobs(handler=_handler, limit=2)

    assert seen == [2], (
        f"the repository was handed {seen}, not the execution limit; the scan "
        f"cap is being applied more than once"
    )


@pytest.mark.asyncio
async def test_the_scan_never_fetches_more_than_the_cap(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], held_locks
) -> None:
    """R29 — measured at the database, with nothing to break out early on.

    Every candidate here is lock-contended, so the execution budget is never
    spent and the loop walks the whole candidate list. ``scanned`` is then the
    true number of rows the query returned.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        recovery_scan_cap,
    )
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    limit = 1
    cap = recovery_scan_cap(limit)
    stale_at = now_kst() - timedelta(hours=6)
    for index in range(cap + 3):
        job_id = await _queue(
            inbox_cleanup, received_at=stale_at - timedelta(minutes=60 - index)
        )
        await _force(job_id, state="processing", attempt_count=1, started_at=stale_at)
        await held_locks.hold(job_id)

    async def _handler(normalized, **kwargs):  # pragma: no cover - all contended
        return {"handled": True, "reason": "approved"}

    report = await recover_callback_jobs(handler=_handler, limit=limit)

    assert report["scanned"] <= cap, (
        f"the scan fetched {report['scanned']} candidates for a cap of {cap}"
    )
    assert report["statuses"].get("lock_contended", 0) == report["scanned"], report


@pytest.mark.asyncio
async def test_ties_on_received_at_are_broken_deterministically(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R29 — identical timestamps must not make the order arbitrary.

    Rows arriving in the same instant are ordinary: a burst of clicks, or a
    backfill. Ordering by ``received_at`` alone leaves PostgreSQL free to
    return them in any order, and a tier's share of the scan could then hold a
    different subset every tick -- so a row could be skipped indefinitely
    without anything looking wrong.
    """
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    same_instant = now_kst() - timedelta(hours=4)
    tied = [await _queue(inbox_cleanup) for _ in range(6)]
    for job_id in tied:
        await _force(job_id, received_at=same_instant)

    orders = []
    for _ in range(3):
        async with AsyncSessionLocal() as session:
            orders.append(
                await CallbackInboxService(session).claimable_job_ids(
                    now=now_kst(), limit=50
                )
            )
            await session.rollback()

    assert orders[0] == orders[1] == orders[2], "tied rows came back in a new order"

    positions = {job_id: orders[0].index(job_id) for job_id in tied}
    by_position = sorted(positions, key=lambda job_id: positions[job_id])
    assert by_position == sorted(tied, key=str), (
        "tied rows are not broken by a stable secondary key"
    )


# ---------------------------------------------------------------------------
# bounded rows is not the same as bounded work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_candidate_scan_is_bounded_in_the_database_too(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R29 — the tiers must be bounded SELECTs, not a window over the backlog.

    Ranking with ``row_number() OVER (PARTITION BY tier ...)`` and filtering
    on the rank classifies and sorts *every* eligible row, in one query, to
    produce a handful of ids.

    What this test claims is exactly what it checks, and no more: at most
    three statements, each separately ``LIMIT``ed, and no full-partition
    ranking. It does **not** claim the database stops reading at the quota.
    ``EXPLAIN`` on a tier query shows ``Limit -> Sort(received_at, job_id) ->
    Index Scan using ix_telegram_callback_inbox_state_available``: the
    predicate uses the index, but the ordering still sorts the eligible set
    (as a bounded-memory top-N), because no index matches that order. Making
    the read itself stop early would need an index built for these predicates
    and this ordering, and that needs its own evidence -- not a claim smuggled
    into a docstring here.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        recovery_scan_cap,
        recovery_tier_quotas,
    )
    from app.services.order_proposals.callback_inbox.repository import (
        CallbackInboxRepository,
    )

    limit = 2
    cap = recovery_scan_cap(limit)
    quotas = recovery_tier_quotas(limit)
    assert sum(quotas.values()) <= cap, quotas

    for index in range(8):
        await _queue(
            inbox_cleanup, received_at=now_kst() - timedelta(minutes=60 - index)
        )

    statements: list[str] = []

    async with AsyncSessionLocal() as session:
        original_execute = session.execute

        async def _recording(statement, *args, **kwargs):
            statements.append(str(statement))
            return await original_execute(statement, *args, **kwargs)

        session.execute = _recording  # type: ignore[method-assign]
        rows = await CallbackInboxRepository(session).claimable_job_ids(
            now=now_kst(),
            stale_before=now_kst() - timedelta(hours=1),
            limit=limit,
        )
        await session.rollback()

    assert len(rows) <= cap, len(rows)
    assert statements, "no query was issued at all"
    assert len(statements) <= 3, (
        f"{len(statements)} queries for three tiers: one bounded SELECT each, "
        f"or one bounded UNION ALL"
    )
    for sql in statements:
        lowered = sql.lower()
        assert "row_number" not in lowered, (
            "ranking over the whole eligible set makes the database do "
            "unbounded work even though it returns bounded rows"
        )
        assert "over (" not in lowered, sql
        assert "limit" in lowered, f"an unbounded SELECT in the candidate scan: {sql}"


# ---------------------------------------------------------------------------
# the fairness ordering is a pure function, wherever it lives
# ---------------------------------------------------------------------------


def test_the_fairness_ordering_keeps_no_state_anywhere() -> None:
    """R29 — including the service, which is where the interleave actually is.

    The earlier structural guard scanned the recovery and repository modules
    and skipped classes, and ``importlib.reload(recovery)`` leaves the service
    module loaded -- so a counter on the service, on its class, or captured in
    a closure would have passed everything.
    """
    import ast
    import inspect

    from app.services.order_proposals.callback_inbox import (
        recovery as recovery_module,
    )
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox import service as service_module
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    # 1. no mutable state at module level, and none on the classes either.
    for module in (recovery_module, repo_module, service_module):
        for name, value in vars(module).items():
            if name.startswith("__"):
                continue
            if inspect.isclass(value):
                if value.__module__ != module.__name__:
                    continue
                for attribute, attribute_value in vars(value).items():
                    if attribute.startswith("__"):
                        continue
                    assert not isinstance(attribute_value, list | dict | set), (
                        f"{module.__name__}.{name}.{attribute} is mutable class state"
                    )
                continue
            assert not isinstance(value, list | dict | set), (
                f"{module.__name__}.{name} is mutable module state; fairness must "
                f"come from the database, not from this process"
            )

    # 2. the ordering method itself writes nothing outside its own locals.
    tree = ast.parse(inspect.getsource(CallbackInboxService.claimable_job_ids).strip())
    for node in ast.walk(tree):
        assert not isinstance(node, ast.Global | ast.Nonlocal), ast.dump(node)
        targets: list[ast.expr] = []
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
        elif isinstance(node, ast.AugAssign | ast.AnnAssign):
            targets = [node.target]
        for target in targets:
            assert not isinstance(target, ast.Attribute), (
                f"the ordering writes to {ast.dump(target)}; it must be a pure "
                f"function of the rows the query returned"
            )


@pytest.mark.asyncio
async def test_a_fresh_service_and_session_produce_the_same_order(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R29 — behaviourally, across instances as well as across processes."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    base = now_kst() - timedelta(hours=7)
    for index in range(5):
        await _queue(inbox_cleanup, received_at=base + timedelta(seconds=index))

    orders = []
    for _ in range(3):
        async with AsyncSessionLocal() as session:
            orders.append(
                await CallbackInboxService(session).claimable_job_ids(
                    now=now_kst(), limit=50
                )
            )
            await session.rollback()

    assert orders[0] == orders[1] == orders[2], (
        "a new service on a new session saw a different order"
    )
