"""ROB-1286 B3 — the DB read seam the tick polls.

r1 had no poll step at all: AC1 constructed ``CandidateEvent`` objects
directly, so the dry replay began *after* the only place a real deployment
could go wrong about which rows it sees. This module is that missing seam.

It is the **single** file in the package permitted to import
``InvestmentReportsRepository``, and it may call exactly one method on it
(``list_events_by_delivery_status``, which is read-only by construction --
a ``SELECT`` with no mutation path). ``test_invariants.py`` enforces both
halves with an AST check, which is a tighter guarantee than r1's blanket
import ban: r1 forbade the module and therefore also forbade the read,
which is why the poll was missing.

Nothing here writes. There is no ``add``/``commit``/``flush``/``delete``
call, no consumption marking, and no ``UPDATE`` of ``outcome``,
``delivery_status`` or ``follow_up_report_item_id`` -- consumption lives in
the claim store precisely so this flow never has to mutate
``review.investment_watch_events``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from app.services.investment_reports.repository import InvestmentReportsRepository

__all__ = [
    "DatabaseWatchEventSource",
    "WatchEventRow",
    "WatchEventSource",
]


class WatchEventRow(Protocol):
    """The subset of ``review.investment_watch_events`` the poll reads.

    Structural, so the ORM model satisfies it without this package
    importing it, and a test row does too.
    """

    event_uuid: Any
    symbol: str
    market: str
    outcome: str
    delivery_status: str
    delivered_at: datetime | None
    metric: str
    operator: str
    threshold: Any
    threshold_high: Any
    threshold_key: str
    current_value: Any
    created_at: datetime


class WatchEventSource(Protocol):
    """Port: where delivered watch fires come from."""

    async def fetch_delivered(
        self,
        *,
        market: str | None,
        delivered_since: datetime | None,
        limit: int,
    ) -> Sequence[WatchEventRow]: ...


class DatabaseWatchEventSource:
    """Reads delivered fires from ``review.investment_watch_events``.

    Takes a session factory rather than a session so one tick owns one
    short-lived read transaction and holds no connection between ticks.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    async def fetch_delivered(
        self,
        *,
        market: str | None,
        delivered_since: datetime | None,
        limit: int,
    ) -> Sequence[WatchEventRow]:
        async with self._session_factory() as session:
            repository = InvestmentReportsRepository(session)
            return await repository.list_events_by_delivery_status(
                delivery_status="delivered",
                delivered_since=delivered_since,
                market=market,
                limit=limit,
            )
