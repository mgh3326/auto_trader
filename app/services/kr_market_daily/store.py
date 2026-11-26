"""Insert-only persistence for market-wide KR daily facts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_market_daily import (
    KRMarketBreadth,
    KRMarketDepositCredit,
    KRMarketInvestorNet,
)


async def insert_market_daily(
    session: AsyncSession,
    *,
    breadth: Sequence[Mapping[str, Any]],
    investors: Sequence[Mapping[str, Any]],
    deposits: Sequence[Mapping[str, Any]],
    first_received_at: datetime,
) -> dict[str, int]:
    """Insert source observations once; an existing natural key is immutable."""
    result_counts: dict[str, int] = {}
    for name, model, rows in (
        ("breadth", KRMarketBreadth, breadth),
        ("investors", KRMarketInvestorNet, investors),
        ("deposits", KRMarketDepositCredit, deposits),
    ):
        if not rows:
            result_counts[name] = 0
            continue
        values = [
            {
                **row,
                "first_received_at": row.get("first_received_at", first_received_at),
            }
            for row in rows
        ]
        result = await session.execute(
            insert(model).values(values).on_conflict_do_nothing()
        )
        result_counts[name] = int(result.rowcount or 0)
    await session.flush()
    return result_counts
