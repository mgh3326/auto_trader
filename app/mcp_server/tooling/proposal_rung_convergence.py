"""ROB-1284 — shared reconcile-side wiring for the phantom-resting rung sweep.

The three live reconcile kernels (KIS KR, Toss, US/crypto) each project broker
evidence onto proposal rungs, but all three project *from a ledger row they are
already iterating*.  A rung whose ledger row is terminal-and-therefore-no-longer-
scanned, or whose ledger row never existed, is unreachable from that direction —
the gap ``live_order_ledger._converge_proposal_rung`` documents as "a
guaranteed-convergence proposal-rung reconcile sweep is tracked as follow-up".

This module is the wiring for that sweep.  It runs from the rung side, over the
whole population (no limit, no window), on every non-dry-run reconcile pass, so
DAY expiry that reached the ledger converges into rung state on *the next
reconcile* rather than never.

Deliberately additive: no scheduler is registered here.  This piggybacks on the
reconcile passes that already run; it creates no new recurrence of its own.
"""

from __future__ import annotations

import datetime
import logging
from typing import Any
from typing import cast as typing_cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal
from app.services.order_proposals.resting_sweep_service import RestingRungSweepService

logger = logging.getLogger(__name__)

__all__ = ["candidate_scan_coverage", "run_resting_rung_sweep", "scanned_row_bounds"]

# Every open-row candidate scan in the three reconcile kernels uses this order.
_SCAN_ORDER = "created_at ASC (oldest-first)"


def scanned_row_bounds(
    rows: list[Any],
) -> tuple[datetime.datetime | None, datetime.datetime | None]:
    """(oldest, newest) ``created_at`` across the rows a pass actually scanned.

    Computed from the rows already in hand — no extra query — and by min/max
    rather than by index, because one caller (Toss) merges reopened rows into
    the work-list and so cannot assume the list stayed sorted.
    """
    stamps = [
        row.created_at for row in rows if getattr(row, "created_at", None) is not None
    ]
    if not stamps:
        return None, None
    return min(stamps), max(stamps)


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return typing_cast(
        async_sessionmaker[AsyncSession], typing_cast(object, AsyncSessionLocal)
    )


async def run_resting_rung_sweep(*, dry_run: bool) -> dict[str, Any]:
    """Converge every rung that has committed terminal broker evidence.

    Returns a compact summary for the reconcile payload.  Evidence-first
    throughout: ``NO_EVIDENCE`` and ``CONFLICT`` rungs are counted and reported,
    never transitioned.

    A sweep failure is reported in the payload (``"error"``), not raised — the
    ledger booking the caller just performed stays authoritative — but it is
    also logged at ERROR so a persistently failing sweep is alertable rather
    than a silently empty section.
    """
    now = datetime.datetime.now(datetime.UTC)
    try:
        async with _session_factory()() as db:
            service = RestingRungSweepService(db)
            result = await service.apply(now=now, dry_run=dry_run, confirm=not dry_run)
            if not dry_run:
                await db.commit()
    except Exception as exc:  # noqa: BLE001 - never fail the caller's reconcile
        logger.error(
            "ROB-1284 resting rung sweep failed (dry_run=%s): %s", dry_run, exc
        )
        return {"ran": False, "error": str(exc) or exc.__class__.__name__}
    return {
        "ran": True,
        "dry_run": dry_run,
        "summary": result["summary"],
        "applied": result["applied"],
        "failed": result["failed"],
    }


def candidate_scan_coverage(
    *,
    scanned: int,
    open_total: int | None,
    limit: int,
    oldest_scanned_at: datetime.datetime | None = None,
    newest_scanned_at: datetime.datetime | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Describe what a limited candidate scan did NOT look at (AC3).

    The reconcile candidate scans order by ``created_at ASC`` and cut at
    ``limit``.  When the open population exceeds the limit, the *oldest* rows
    occupy every slot on every pass — and those are precisely the rows already
    past the broker's lookback window, so they never resolve and never yield
    their slot.  Newer, resolvable rows are then never scanned at all.

    Silently returning "reconciled N" while M rows were never looked at reads as
    full coverage.  This makes the shortfall explicit in the payload.

    The shortfall is reported as **starvation, not backlog**, because that is
    what it measures.  A backlog drains: next pass reaches what this pass
    missed.  This does not — the scan re-selects the same oldest rows every
    time, so the unreached set is stable and the newest rows are unreachable for
    as long as the slot holders stay open.  Two extra facts make that legible
    without a second query, when the caller can supply them:

    * ``unreached_created_after`` — the newest ``created_at`` this pass actually
      reached.  Every open row newer than that was not looked at, this pass or
      any pass with the same limit.
    * ``oldest_scanned_age_days`` — how long the row holding slot #1 has been
      sitting there.  A large number is the direct evidence that the holders do
      not turn over.

    A fair-rotation scan (or a per-row last-attempt column) is the actual fix and
    needs a migration, so it is out of this PR's scope; surfacing the number is
    what keeps the gap from reading as coverage in the meantime.
    """
    if open_total is None:
        return {
            "scanned": scanned,
            "limit": limit,
            "open_total": None,
            "truncated": None,
            "scan_order": _SCAN_ORDER,
        }
    unscanned = max(0, open_total - scanned)
    coverage: dict[str, Any] = {
        "scanned": scanned,
        "limit": limit,
        "open_total": open_total,
        "unscanned": unscanned,
        "truncated": unscanned > 0,
        "scan_order": _SCAN_ORDER,
    }
    if unscanned == 0:
        return coverage

    frontier = newest_scanned_at.isoformat() if newest_scanned_at else None
    age_days: int | None = None
    if oldest_scanned_at is not None and now is not None:
        age_days = max(0, (now - oldest_scanned_at).days)
    coverage["unreached_created_after"] = frontier
    coverage["oldest_scanned_age_days"] = age_days
    coverage["note"] = (
        f"{unscanned} open row(s) were never scanned this pass (limit={limit}); "
        f"the scan is {_SCAN_ORDER}, so the same oldest rows refill every slot "
        "on every pass and this shortfall is persistent, not a draining backlog"
        + (f" — nothing created after {frontier} is reached" if frontier else "")
        + (
            f", while the row holding slot #1 has been open {age_days} day(s)"
            if age_days is not None
            else ""
        )
        + ". Raising `limit` is required for them to be reached at all; fair "
        "rotation (a per-row last-attempt column) needs a migration and is "
        "tracked separately."
    )
    return coverage
