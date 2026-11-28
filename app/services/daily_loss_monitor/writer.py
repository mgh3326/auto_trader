"""The only system writer of the kill row: ACTIVE -> DAILY_LOSS_FREEZE.

It never unfreezes: the UPDATE matches ``mode = 'ACTIVE'`` only, and the DB
guard (hk 7119 migration) refuses any other system transition. Unfreezing stays
the operator command (``scripts/execution_mandates.py control``).
"""

from __future__ import annotations

from typing import Final

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_mandates import (
    DAILY_LOSS_FREEZE_MODE,
    ExecutionAccountControl,
)

SYSTEM_WRITER: Final = "system:daily_loss_monitor"


async def freeze_if_active(session: AsyncSession, *, account: str, reason: str) -> bool:
    """True when this call moved the row from ACTIVE. The caller commits."""
    why = reason.strip()[:500] or DAILY_LOSS_FREEZE_MODE
    result = await session.execute(
        update(ExecutionAccountControl)
        .where(
            ExecutionAccountControl.account == account,
            ExecutionAccountControl.mode == "ACTIVE",
        )
        .values(
            mode=DAILY_LOSS_FREEZE_MODE,
            reason=why,
            updated_by=SYSTEM_WRITER,
            updated_at=func.now(),
        )
        .returning(ExecutionAccountControl.account)
    )
    return result.scalar_one_or_none() is not None
