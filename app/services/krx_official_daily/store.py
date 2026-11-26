"""Sole writer for ``public.krx_official_daily`` (hk 6586).

Insert-only: an existing (symbol, session_date) row is never updated, so the
first received observation and its ``first_received_at`` survive every re-run.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.krx_official_daily import KRXOfficialDaily


async def insert_official_daily(
    session: AsyncSession,
    *,
    rows: Sequence[Mapping[str, Any]],
    first_received_at: datetime,
) -> int:
    """Insert rows once; returns the number of newly inserted rows."""
    if not rows:
        return 0
    values = [{**row, "first_received_at": first_received_at} for row in rows]
    result = await session.execute(
        insert(KRXOfficialDaily)
        .values(values)
        .on_conflict_do_nothing(index_elements=["symbol", "session_date"])
    )
    await session.flush()
    return int(result.rowcount or 0)
