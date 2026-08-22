"""The safety net for a lost Redis kick.

Reserves a durable cross-tier start, then scans for work the queue may have
dropped -- malformed active budgets, exhausted retries, canonical ``pending``
and due ``retry_wait`` rows, plus canonical ``processing`` rows old enough to
suspect -- and runs each through exactly the same :func:`process_callback_job`
the per-job task uses. It gets a wider set of claimable states and nothing
else: the advisory lock still decides whether a job may be touched, so a live
worker is never overtaken and a "stale" row whose lock is held is simply
skipped.

The report is aggregate-only by design. Counts by state and one age; the only
identifier that appears anywhere is the opaque job UUID, and only in logs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals.callback_inbox.contracts import (
    RECOVERY_CLAIMABLE_STATES,
    RECOVERY_SCAN_LIMIT,
)
from app.services.order_proposals.callback_inbox.result_boundary import (
    NON_CLAIMED_RECOVERY_ITEM_STATUSES,
    empty_recovery_statuses,
    recovery_item_status,
)
from app.services.order_proposals.callback_inbox.service import CallbackInboxService
from app.services.order_proposals.callback_inbox.worker import process_callback_job

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


async def reserve_recovery_tier_block(
    *,
    limit: int,
    session_factory: Callable[[], Any] | None = None,
) -> int:
    """Commit one cyclic recovery block before any candidate scan begins.

    A successful commit may be followed by a process death before scanning;
    burning that block is safe. Conversely, a reservation or commit failure
    is intentionally propagated so the sweep cannot fall back to a fixed,
    time-derived, or process-local ordering.
    """
    if limit < 1:
        raise ValueError("recovery limit must be at least 1")

    factory = session_factory or AsyncSessionLocal
    async with factory() as session:
        tier_start = await CallbackInboxService(session).reserve_recovery_tier_block(
            limit=limit
        )
        await session.commit()
    return tier_start


async def recover_callback_jobs(
    *,
    now_fn: Clock | None = None,
    limit: int = RECOVERY_SCAN_LIMIT,
    session_factory: Callable[[], Any] | None = None,
    process_fn: Callable[..., Any] | None = None,
    **worker_kwargs: Any,
) -> dict[str, Any]:
    """One sweep. Bounded, idempotent, and safe to run concurrently.

    ``limit`` is the **execution** cap: how many jobs this tick may actually
    claim and run. The **scan** cap is derived from it and is larger, because
    a candidate whose advisory lock is held costs one failed
    ``pg_try_advisory_lock`` and nothing else.

    They were the same number until R29, and that was a liveness bug rather
    than a tuning choice: the scan ordered by age, so a few long-running jobs
    under live worker locks sat at the front of every tick, filled the budget
    with ``lock_contended``, and the pending row behind them -- the lost Redis
    kick this sweep exists to recover -- was never selected. Not on that tick,
    and not on any tick, for as long as those workers ran.

    The database decides which candidates exist and in what order within each
    tier: the tier predicates, the per-tier quotas and the ``received_at``,
    ``job_id`` ordering are all evaluated there. Before this scan, a separate
    short transaction atomically commits a PII-free cursor reservation. Its
    exact start is passed through unchanged; there is no local, clock-derived,
    or fallback ordering state. The budget is spent per *claim*.
    """
    if limit < 1:
        raise ValueError("recovery limit must be at least 1")

    clock = now_fn or now_kst
    factory = session_factory or AsyncSessionLocal
    process = process_fn or process_callback_job

    # The reservation is its own committed transaction. Do not catch an error
    # here: commit ambiguity must fail closed before a scan or handler.
    tier_start = await reserve_recovery_tier_block(
        limit=limit,
        session_factory=factory,
    )

    async with factory() as session:
        service = CallbackInboxService(session)
        # The *execution* limit crosses the boundary, exactly once. The scan
        # cap and the per-tier quotas are derived from it in one place, at the
        # query; deriving one here and handing that down applied the same
        # multiplier twice (R29).
        candidates = await service.claimable_job_ids(
            now=clock(),
            limit=limit,
            tier_start=tier_start,
        )
        await session.rollback()

    # Keep the aggregate report closed even when a sweep looks at no rows.
    # The boundary module owns this vocabulary so a future internal worker
    # result cannot silently introduce a serializable status bucket.
    statuses = empty_recovery_statuses()
    claimed = 0
    scanned = 0
    for database_job_id in candidates:
        if claimed >= limit:
            break
        scanned += 1
        job_id = _materialize_trusted_candidate_uuid(database_job_id)
        if job_id is None:
            # A malformed repository value still consumes one bounded claim,
            # but never crosses the item-processing authority boundary.
            result = "error"
        else:
            result = await _process_one(
                job_id, process_fn=process, now_fn=clock, worker_kwargs=worker_kwargs
            )
        statuses[result] = statuses.get(result, 0) + 1
        # A job someone else is holding, or that turned out not to be
        # claimable, cost a look. Only work actually done costs budget.
        if result not in NON_CLAIMED_RECOVERY_ITEM_STATUSES:
            claimed += 1

    now = clock()
    async with factory() as session:
        backlog = await CallbackInboxService(session).backlog(now=now)
        await session.rollback()

    logger.info(
        "order_proposals.telegram.callback_recovery_swept",
        extra={
            "callback_recovery.scanned": scanned,
            "callback_recovery.claimed": claimed,
            "callback_recovery.pending": backlog["pending"],
            "callback_recovery.processing": backlog["processing"],
            "callback_recovery.retry_wait": backlog["retry_wait"],
            "callback_recovery.dead_letter": backlog["dead_letter"],
        },
    )
    return {
        "status": "ok",
        # How many candidates this tick actually looked at. Bounded by
        # ``recovery_scan_cap(limit)``, and reported so that bound is
        # observable rather than merely asserted in a docstring.
        "scanned": scanned,
        "claimed": claimed,
        "statuses": statuses,
        "backlog": backlog,
    }


def _materialize_trusted_candidate_uuid(value: object) -> uuid.UUID | None:
    """Copy only exact stdlib or asyncpg UUID storage into a stdlib UUID.

    The exact ``uuid.UUID`` representation contributes only its own ``int``
    descriptor; the exact ``asyncpg.pgproto.pgproto.UUID`` representation
    contributes only its own ``bytes`` descriptor. Every other object,
    including arbitrary Python UUID subclasses, is rejected without parsing
    or rendering it.
    """
    value_type = type(value)
    if value_type is uuid.UUID:
        try:
            raw_int = uuid.UUID.int.__get__(value, uuid.UUID)
        except (AttributeError, TypeError, ValueError):
            return None
        if type(raw_int) is not int:
            return None
        try:
            return uuid.UUID(int=raw_int)
        except (AttributeError, TypeError, ValueError):
            return None

    from asyncpg.pgproto import pgproto

    if value_type is pgproto.UUID:
        try:
            raw_bytes = pgproto.UUID.bytes.__get__(value, pgproto.UUID)
        except (AttributeError, TypeError, ValueError):
            return None
        if type(raw_bytes) is not bytes or len(raw_bytes) != 16:
            return None
        try:
            return uuid.UUID(bytes=raw_bytes)
        except (AttributeError, TypeError, ValueError):
            return None
    else:
        return None


async def _process_one(
    job_id: uuid.UUID,
    *,
    process_fn: Callable[..., Any],
    now_fn: Clock,
    worker_kwargs: dict[str, Any],
) -> str:
    """One job's failure must not end the sweep for the others."""
    if type(job_id) is not uuid.UUID:
        return "error"

    canonical_job_id = str(job_id)
    try:
        result = await process_fn(
            job_id,
            now_fn=now_fn,
            claimable_states=RECOVERY_CLAIMABLE_STATES,
            **worker_kwargs,
        )
        status = recovery_item_status(result, job_id=canonical_job_id)
    except Exception:  # noqa: BLE001 - keep sweeping; BaseException propagates
        try:
            logger.error(
                "order_proposals.telegram.callback_recovery_job_failed",
                extra={"callback_job.id": canonical_job_id},
            )
        except Exception:  # noqa: BLE001 - logging must not end a sweep
            pass
        return "error"
    return status or "error"


__all__ = ["recover_callback_jobs", "reserve_recovery_tier_block"]
