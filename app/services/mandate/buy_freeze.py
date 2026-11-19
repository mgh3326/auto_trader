"""Fail-closed drawdown interface until hk 4900 supplies measurements."""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class FreezeVerdict:
    frozen: bool
    reason: str
    evidence: dict[str, Any]
    observed_version: str | None
    valid_until: datetime | None


async def buy_freeze_state(
    session: AsyncSession,
    *,
    account_mode: str,
    market: str,
    sleeve_id: int | None,
    now: datetime,
) -> FreezeVerdict:
    """No measurement is authority to buy; this stub blocks every account."""
    return FreezeVerdict(
        frozen=True,
        reason="measurement_unavailable",
        evidence={},
        observed_version=None,
        valid_until=None,
    )
