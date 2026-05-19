"""Watch-context snapshot collector (read-only).

Reads currently-active ``investment_watch_alerts`` for the requested
market. The collector never activates, expires, or transitions watches —
the only allowed write path is :class:`WatchActivationService` which is
deliberately not imported here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    utcnow,
)
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)


class WatchContextSnapshotCollector:
    """Required-kind ``watch_context`` collector backed by ``investment_watch_alerts``."""

    snapshot_kind: str = "watch_context"

    def __init__(
        self,
        session: AsyncSession,
        *,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def collect(
        self, request: CollectorRequest
    ) -> list[SnapshotCollectResult]:
        now = utcnow()
        alerts = await self._repo.list_active_alerts(
            market=request.market, valid_at=now
        )

        payload: dict[str, Any] = {
            "active_alerts": [_alert_to_dict(a) for a in alerts],
            "active_count": len(alerts),
            "market": request.market,
        }
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="auto_trader_db",
                as_of=now,
                coverage={"active_count": len(alerts)},
            )
        ]


def _alert_to_dict(a: Any) -> dict[str, Any]:
    return {
        "alert_uuid": a.alert_uuid,
        "source_report_uuid": a.source_report_uuid,
        "source_item_uuid": a.source_item_uuid,
        "market": a.market,
        "symbol": a.symbol,
        "metric": a.metric,
        "operator": a.operator,
        "threshold": a.threshold,
        "threshold_key": a.threshold_key,
        "intent": a.intent,
        "action_mode": a.action_mode,
        "rationale": a.rationale,
        "valid_until": a.valid_until,
        "status": a.status,
        "activated_at": a.activated_at,
    }
