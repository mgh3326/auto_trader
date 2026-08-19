"""ROB-1286 B3 — poll: DB rows -> candidate events.

The mapping half of the poll seam, kept pure and separate from
:mod:`.event_source` so the row translation is testable without a session
and so the DB adapter stays a four-line call.

Two things happen here that a caller must not have to remember:

``row -> CandidateEvent``
    ``event_uuid`` is a ``uuid.UUID`` on the ORM model and a ``str``
    everywhere in this package (it is the claim key, and a claim key that
    is sometimes a UUID and sometimes its string form is two different
    keys). It is normalised once, here.
``dedup by event_uuid``
    A poll can legitimately return the same fire twice -- overlapping
    ``delivered_since`` cursors across ticks, or a retried delivery. The
    claim store would catch it, but only after the row had already
    consumed a round-cap slot, silently displacing a different symbol's
    fire into overflow. Deduping at the poll keeps the cap meaning "three
    distinct fires".

Ordering is preserved as the repository returned it (``delivered_at``
ascending, oldest fire first), because when the cap bites, the oldest
unhandled fire is the one that has been waiting longest.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from app.services.watch_trigger_repricing.event_source import (
    WatchEventRow,
    WatchEventSource,
)
from app.services.watch_trigger_repricing.selection import CandidateEvent

__all__ = [
    "DEFAULT_POLL_LIMIT",
    "poll_candidate_events",
    "to_candidate_event",
]

# Bounds one tick's read. Well above the round cap on purpose: the tick
# must *see* everything it is deferring in order to report it, even though
# it will only act on ``round_cap`` of them.
DEFAULT_POLL_LIMIT = 50


def to_candidate_event(row: WatchEventRow) -> CandidateEvent:
    """Translate one DB row into the flow's read-only view of it."""
    return CandidateEvent(
        event_uuid=str(row.event_uuid),
        symbol=row.symbol,
        market=row.market,
        outcome=row.outcome,
        delivery_status=row.delivery_status,
        delivered_at=row.delivered_at,
    )


def dedupe_candidates(
    candidates: Sequence[CandidateEvent],
) -> list[CandidateEvent]:
    """First occurrence wins, order preserved."""
    seen: set[str] = set()
    unique: list[CandidateEvent] = []
    for candidate in candidates:
        if candidate.event_uuid in seen:
            continue
        seen.add(candidate.event_uuid)
        unique.append(candidate)
    return unique


async def poll_candidate_events(
    source: WatchEventSource,
    *,
    market: str | None = "kr",
    delivered_since: datetime | None = None,
    limit: int = DEFAULT_POLL_LIMIT,
) -> list[CandidateEvent]:
    """Read delivered fires and hand back de-duplicated candidates.

    Filtering by ``outcome`` is deliberately **not** done here. Selection
    owns it (:func:`.selection.select_candidates`) so that every row the
    poll saw is accounted for in the tick report with a named reason --
    dropping non-``review_required`` rows silently at the poll would make
    them invisible, which is the failure mode this whole issue exists to
    remove.
    """
    rows = await source.fetch_delivered(
        market=market,
        delivered_since=delivered_since,
        limit=limit,
    )
    return dedupe_candidates([to_candidate_event(row) for row in rows])
