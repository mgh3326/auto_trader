"""Sole writer for ``public.krx_official_daily`` (hk 6586).

Insert-only: an existing (symbol, session_date) row is never updated, so the
first received observation and its ``first_received_at`` survive every re-run.
Rows are written in bounded chunks inside the caller's transaction so that one
full two-market collection stays under asyncpg's 32,767 bound-parameter limit.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.krx_official_daily import KRXOfficialDaily

# 16 bound columns per row (including first_received_at): 1000 rows = 16,000 parameters.
CHUNK_ROWS = 1000


async def insert_official_daily(
    session: AsyncSession,
    *,
    rows: Sequence[Mapping[str, Any]],
    first_received_at: datetime,
) -> int:
    """Insert rows once; returns the total number of newly inserted rows."""
    inserted = 0
    for start in range(0, len(rows), CHUNK_ROWS):
        chunk = [
            {**row, "first_received_at": first_received_at}
            for row in rows[start : start + CHUNK_ROWS]
        ]
        result = await session.execute(
            insert(KRXOfficialDaily)
            .values(chunk)
            .on_conflict_do_nothing(index_elements=["symbol", "session_date"])
        )
        inserted += int(result.rowcount or 0)
    await session.flush()
    return inserted
