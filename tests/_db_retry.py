"""Shared deadlock-retry helper for the xdist-shared test DB (ROB-723).

Under ``--dist=loadfile`` multiple workers share one PostgreSQL ``test_db``.
TRUNCATE/DDL (AccessExclusive) can still lose a lock-order race to another
worker's activity and be chosen as the deadlock victim. Those operations are
idempotent, so rollback + retry is safe. Generalizes the ad-hoc retry in
``tests/services/test_paper_retrospective_pending.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.exc import DBAPIError


def _is_deadlock(exc: DBAPIError) -> bool:
    return "deadlock" in str(exc).lower()


async def run_with_deadlock_retry(
    op: Callable[[], Awaitable[Any]],
    *,
    rollback: Callable[[], Awaitable[Any]] | None = None,
    attempts: int = 6,
    base_delay: float = 0.05,
) -> Any:
    """Run ``op`` retrying only on Postgres deadlock (SQLSTATE 40P01).

    Re-raises any non-deadlock ``DBAPIError`` immediately. Between attempts it
    awaits ``rollback`` (if provided) and backs off ``base_delay * 2**n``.
    """
    last: DBAPIError | None = None
    for n in range(attempts):
        try:
            return await op()
        except DBAPIError as exc:
            if not _is_deadlock(exc):
                raise
            last = exc
            if rollback is not None:
                await rollback()
            if n < attempts - 1 and base_delay:
                await asyncio.sleep(base_delay * (2**n))
    assert last is not None  # pragma: no cover - only true when attempts == 0
    raise last
