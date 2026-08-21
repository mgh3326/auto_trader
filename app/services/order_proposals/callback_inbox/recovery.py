"""The safety net for a lost Redis kick.

Scans for work the queue may have dropped -- ``pending`` and due ``retry_wait``
rows, plus ``processing`` rows old enough to suspect -- and runs each through
exactly the same :func:`process_callback_job` the per-job task uses. It gets a
wider set of claimable states and nothing else: the advisory lock still
decides whether a job may be touched, so a live worker is never overtaken and
a "stale" row whose lock is held is simply skipped.

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
from app.services.order_proposals.callback_inbox.service import CallbackInboxService
from app.services.order_proposals.callback_inbox.worker import process_callback_job

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]


async def recover_callback_jobs(
    *,
    now_fn: Clock | None = None,
    limit: int = RECOVERY_SCAN_LIMIT,
    session_factory: Callable[[], Any] | None = None,
    process_fn: Callable[..., Any] | None = None,
    **worker_kwargs: Any,
) -> dict[str, Any]:
    """One sweep. Bounded, idempotent, and safe to run concurrently."""
    clock = now_fn or now_kst
    factory = session_factory or AsyncSessionLocal
    process = process_fn or process_callback_job

    async with factory() as session:
        service = CallbackInboxService(session)
        candidates = await service.claimable_job_ids(now=clock(), limit=limit)
        await session.rollback()

    statuses: dict[str, int] = {}
    claimed = 0
    for job_id in candidates:
        result = await _process_one(
            job_id, process_fn=process, now_fn=clock, worker_kwargs=worker_kwargs
        )
        statuses[result] = statuses.get(result, 0) + 1
        if result not in {"lock_contended", "not_claimable", "not_found"}:
            claimed += 1

    now = clock()
    async with factory() as session:
        backlog = await CallbackInboxService(session).backlog(now=now)
        await session.rollback()

    logger.info(
        "order_proposals.telegram.callback_recovery_swept",
        extra={
            "callback_recovery.scanned": len(candidates),
            "callback_recovery.claimed": claimed,
            "callback_recovery.pending": backlog["pending"],
            "callback_recovery.processing": backlog["processing"],
            "callback_recovery.retry_wait": backlog["retry_wait"],
            "callback_recovery.dead_letter": backlog["dead_letter"],
        },
    )
    return {
        "status": "ok",
        "claimed": claimed,
        "statuses": statuses,
        "backlog": backlog,
    }


async def _process_one(
    job_id: uuid.UUID,
    *,
    process_fn: Callable[..., Any],
    now_fn: Clock,
    worker_kwargs: dict[str, Any],
) -> str:
    """One job's failure must not end the sweep for the others."""
    try:
        result = await process_fn(
            job_id,
            now_fn=now_fn,
            claimable_states=RECOVERY_CLAIMABLE_STATES,
            **worker_kwargs,
        )
    except Exception as exc:  # noqa: BLE001 - keep sweeping
        logger.error(
            "order_proposals.telegram.callback_recovery_job_failed",
            extra={
                "callback_job.id": str(job_id),
                "exception_type": type(exc).__name__,
            },
        )
        return "error"
    return str(result.get("status", "error"))


__all__ = ["recover_callback_jobs"]
