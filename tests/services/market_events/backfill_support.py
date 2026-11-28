"""An in-memory store with the SQL store's transaction behaviour (hk 6409)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from app.services.market_events.backfill.policy import BackfillRequest, build_request

NOW = datetime(2026, 11, 20, 12, tzinfo=UTC)


def make_request(**over: Any) -> BackfillRequest:
    args: dict[str, Any] = {
        "source": "dart",
        "market": "kr",
        "category": None,
        "symbols": [],
        "from_date": date(2026, 11, 1),
        "to_date": date(2026, 11, 20),
        "reason_code": "gap_fill",
        "reason": "outage",
        "requested_by": "operator",
        "now": NOW,
    }
    args.update(over)
    return build_request(**args)


@dataclass(frozen=True)
class Row:
    request_hash: str
    source: str
    state: str
    created_at: datetime
    exit_code: int | None
    failure_code: str | None


class FakeStore:
    """Uncommitted inserts vanish on rollback; the unique key is enforced."""

    def __init__(self, now: datetime = NOW) -> None:
        self.clock = now
        self.rows: list[Row] = []
        self.pending: list[Row] = []
        self.locks: list[str] = []

    async def lock_source(self, source: str) -> datetime:
        self.locks.append(source)
        return self.clock

    async def states_for(self, request_hash: str) -> set[str]:
        return {r.state for r in self.rows if r.request_hash == request_hash}

    async def count(
        self, *, source: str, state: str, since: datetime, until: datetime
    ) -> int:
        return sum(
            1
            for r in self.rows
            if r.source == source and r.state == state and since <= r.created_at < until
        )

    async def insert(
        self,
        request: BackfillRequest,
        *,
        state: str,
        created_at: datetime,
        exit_code: int | None = None,
        failure_code: str | None = None,
    ) -> None:
        for r in [*self.rows, *self.pending]:
            assert (r.request_hash, r.state) != (request.request_hash, state), (
                "unique (request_hash, state)"
            )
        self.pending.append(
            Row(
                request.request_hash,
                request.source,
                state,
                created_at,
                exit_code,
                failure_code,
            )
        )

    async def now(self) -> datetime:
        return self.clock

    async def commit(self) -> None:
        self.rows += self.pending
        self.pending = []

    async def rollback(self) -> None:
        self.pending = []

    def states(self) -> list[str]:
        return [r.state for r in self.rows]
