"""Fail-closed drawdown interface until hk 4900 supplies measurements.

This is the one reader of the buy freeze (design §5.4, I11). The proposal gate, the
send-fence transaction and the pre-send recheck all call ``buy_freeze_state``; hk 4900
replaces the body and keeps the signature. Its writer takes the freeze scope lock F1
exclusively (``fence_locks.acquire_freeze_exclusive``); a send fence holds F1 shared
while it reads this verdict, so a freeze committed first is always seen.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


@dataclass(frozen=True)
class FreezeVerdict:
    frozen: bool
    reason: str
    evidence: dict[str, Any]
    observed_version: str | int | None = None
    valid_until: datetime | None = None


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
