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

# The Toss open-row candidate scan still uses this order.  The KIS KR and
# US/crypto kernels moved to an evidence-reach aging order (ROB-719) and pass
# their own ``scan_order`` text to ``candidate_scan_coverage``.
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
    scan_order: str = _SCAN_ORDER,
    probeable_open: int | None = None,
) -> dict[str, Any]:
    """Describe what a limited candidate scan did NOT look at (AC3).

    The reconcile candidate scans cut at ``limit``.  Without aging the order
    was plain ``created_at ASC`` and the *oldest* rows occupied every slot on
    every pass — precisely the rows already past the broker's lookback window,
    so they never resolved and never yielded their slot.  Newer, resolvable
    rows were then never scanned at all.

    ROB-719 replaced that order with evidence-reach aging on the KIS kernels:
    rows that can still produce broker evidence sort first and rows provably
    beyond the broker's lookback depth fill only leftover slots.  Callers that
    run an aged scan pass ``scan_order`` plus ``probeable_open`` (the number of
    open rows still inside evidence reach) so the shortfall splits into:

    * ``unreached_probeable`` — reachable rows that overflowed the limit.  A
      real backlog: it drains as earlier open rows resolve or age out.
    * ``unreached_beyond_reach`` — rows beyond the broker's documented evidence
      depth, deliberately deprioritized.  They cannot produce evidence; they
      need operator review, not more scan slots.

    When ``probeable_open`` is None the caller could not supply the split (the
    Toss scan still scans oldest-first) and the note keeps describing the
    original persistent-starvation mechanics.

    Silently returning "reconciled N" while M rows were never looked at reads as
    full coverage.  This makes the shortfall explicit in the payload.

    The shortfall is reported as **starvation, not backlog**, because that is
    what it measures.  A backlog drains: next pass reaches what this pass
    missed.  The oldest-first scan did not — it re-selected the same oldest
    rows every time.  Two extra facts make that legible
    without a second query, when the caller can supply them:

    * ``unreached_created_after`` — the newest ``created_at`` this pass actually
      reached.  Every open row newer than that was not looked at, this pass or
      any pass with the same limit.
    * ``oldest_scanned_age_days`` — how long the row holding slot #1 has been
      sitting there.  A large number is the direct evidence that the holders do
      not turn over.
    """
    if open_total is None:
        return {
            "scanned": scanned,
            "limit": limit,
            "open_total": None,
            "truncated": None,
            "scan_order": scan_order,
        }
    unscanned = max(0, open_total - scanned)
    coverage: dict[str, Any] = {
        "scanned": scanned,
        "limit": limit,
        "open_total": open_total,
        "unscanned": unscanned,
        "truncated": unscanned > 0,
        "scan_order": scan_order,
    }
    unreached_probeable: int | None = None
    unreached_beyond_reach: int | None = None
    if probeable_open is not None:
        probeable_scanned = min(scanned, max(0, probeable_open))
        unreached_probeable = max(0, probeable_open - probeable_scanned)
        unreached_beyond_reach = unscanned - unreached_probeable
        coverage["probeable_open"] = probeable_open
        coverage["beyond_evidence_reach"] = max(0, open_total - probeable_open)
        coverage["unreached_probeable"] = unreached_probeable
        coverage["unreached_beyond_reach"] = unreached_beyond_reach
    if unscanned == 0:
        return coverage

    frontier = newest_scanned_at.isoformat() if newest_scanned_at else None
    age_days: int | None = None
    if oldest_scanned_at is not None and now is not None:
        age_days = max(0, (now - oldest_scanned_at).days)
    coverage["unreached_created_after"] = frontier
    coverage["oldest_scanned_age_days"] = age_days
    if probeable_open is None:
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
    else:
        coverage["note"] = (
            f"{unscanned} open row(s) were never scanned this pass (limit={limit}); "
            f"the scan is {scan_order}"
            + (f" — nothing created after {frontier} is reached" if frontier else "")
            + (
                f", while the row holding slot #1 has been open {age_days} day(s)"
                if age_days is not None
                else ""
            )
            + f". {unreached_probeable} unreached row(s) are still inside the "
            "broker evidence window (a real backlog that drains as earlier open "
            f"rows resolve or age out) and {unreached_beyond_reach} sit beyond "
            "the broker evidence reach — permanently unresolvable rows that are "
            "deliberately deprioritized rather than allowed to fill every slot."
        )
    return coverage
