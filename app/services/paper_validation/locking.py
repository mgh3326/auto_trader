"""Shared PostgreSQL lock ordering for ROB-848 validation streams."""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_VALIDATION_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")


async def lock_validation_streams(
    session: AsyncSession, validation_ids: Iterable[str]
) -> None:
    """Lock unique validation streams in one process-independent total order.

    The lifecycle-lock suffix is sorted/deduplicated validation streams, then
    the cohort lock. A claim-owning runner acquires its claim row before this
    suffix; activation and terminal fencing never acquire claim rows.
    """

    for validation_id in sorted(set(validation_ids)):
        await session.execute(_VALIDATION_LOCK_SQL, {"key": validation_id})


__all__ = ["lock_validation_streams"]
