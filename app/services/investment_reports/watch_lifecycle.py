"""Explicit lifecycle transitions for investment watch alerts (ROB-971)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import now_kst
from app.models.investment_reports import InvestmentWatchAlert
from app.services.investment_reports.repository import InvestmentReportsRepository


class WatchLifecycleService:
    """Void/expire watches without bypassing the investment-report service layer."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def void(self, alert_uuid: UUID, *, reason: str) -> InvestmentWatchAlert:
        return await self._transition(
            alert_uuid, target_status="canceled", reason=reason
        )

    async def expire(
        self, alert_uuid: UUID, *, reason: str = "operator_expired"
    ) -> InvestmentWatchAlert:
        return await self._transition(
            alert_uuid, target_status="expired", reason=reason
        )

    async def sweep_expired(self, *, now: datetime) -> list[InvestmentWatchAlert]:
        alerts = await self._repo.list_expired_active_alerts_for_update(now=now)
        for alert in alerts:
            metadata = dict(alert.alert_metadata or {})
            metadata["lifecycle_transition"] = {
                "status": "expired",
                "reason": "valid_until_elapsed",
                "at": now.isoformat(),
            }
            await self._repo.update_alert_lifecycle(
                alert.id, status="expired", metadata=metadata
            )
        await self._session.flush()
        return alerts

    async def _transition(
        self, alert_uuid: UUID, *, target_status: str, reason: str
    ) -> InvestmentWatchAlert:
        normalized_reason = reason.strip()
        if not normalized_reason:
            raise ValueError("reason must not be blank")
        alert = await self._repo.get_alert_by_uuid_for_update(alert_uuid)
        if alert is None:
            raise ValueError(f"watch alert not found: {alert_uuid}")
        if alert.status == target_status:
            return alert
        if alert.status != "active":
            raise ValueError(
                f"cannot transition watch in status={alert.status!r} to {target_status!r}"
            )
        metadata = dict(alert.alert_metadata or {})
        metadata["lifecycle_transition"] = {
            "status": target_status,
            "reason": normalized_reason,
            "at": now_kst().isoformat(),
        }
        await self._repo.update_alert_lifecycle(
            alert.id, status=target_status, metadata=metadata
        )
        await self._session.flush()
        await self._session.refresh(alert)
        return alert
