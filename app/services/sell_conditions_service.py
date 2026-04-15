from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.sell_condition import SellCondition

logger = logging.getLogger(__name__)


async def get_sell_condition(db: AsyncSession, symbol: str) -> SellCondition | None:
    result = await db.execute(
        select(SellCondition).where(SellCondition.symbol == symbol)
    )
    return result.scalar_one_or_none()


async def get_active_sell_conditions(
    db: AsyncSession,
) -> list[SellCondition]:
    result = await db.execute(
        select(SellCondition).where(SellCondition.is_active.is_(True))
    )
    return list(result.scalars().all())
