"""ROB-971 verification follow-up (MED-5) — WatchLifecycleService invariants.

The service's only safety invariant is that ``_transition`` refuses to move
a watch alert that isn't currently ``active`` into a new status (void/expire
are one-shot, not re-appliable to an already-terminal row). Nothing
previously exercised that guard directly: a mutation that deleted it
(``if alert.status != "active": raise ...`` -> no-op) still left the full
suite green. These tests are mutation-sensitive for that specific branch.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.investment_reports import CreateInvestmentWatchRequest
from app.services.investment_reports.watch_create import DirectWatchCreateService
from app.services.investment_reports.watch_lifecycle import WatchLifecycleService
from tests._investment_reports_helpers import future_datetime


async def _create_active_alert(session: AsyncSession, *, symbol: str):
    request = CreateInvestmentWatchRequest.model_validate(
        {
            "created_by": "test",
            "market": "kr",
            "symbol": symbol,
            "intent": "sell_review",
            "rationale": "ROB-971 lifecycle invariant fixture",
            "watch_condition": {"metric": "price", "operator": "below", "threshold": 1},
            "valid_until": future_datetime().isoformat(),
        }
    )
    alert, _ = await DirectWatchCreateService(session).create(request)
    await session.commit()
    return alert


@pytest.mark.asyncio
async def test_transition_rejects_non_active_alert(session: AsyncSession) -> None:
    """A canceled watch must not be re-transitioned to expired (or vice
    versa) — status transitions are one-shot from ``active`` only."""
    alert = await _create_active_alert(session, symbol="R971LC01")
    service = WatchLifecycleService(session)

    await service.void(alert.alert_uuid, reason="first transition")

    with pytest.raises(ValueError, match="cannot transition watch in status"):
        await service.expire(alert.alert_uuid, reason="second transition")


@pytest.mark.asyncio
async def test_void_is_idempotent_for_same_target_status(
    session: AsyncSession,
) -> None:
    """Re-requesting the *same* target status is a documented no-op, not a
    guard violation — this is the one exception to the active-only rule."""
    alert = await _create_active_alert(session, symbol="R971LC02")
    service = WatchLifecycleService(session)

    first = await service.void(alert.alert_uuid, reason="cleanup")
    second = await service.void(alert.alert_uuid, reason="cleanup again")

    assert first.status == "canceled"
    assert second.status == "canceled"
