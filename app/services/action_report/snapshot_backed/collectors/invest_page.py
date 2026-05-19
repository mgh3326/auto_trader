"""Invest-page snapshot collector (read-only, optional).

Captures a small slice of "what the /invest dashboard would render right
now" — namely the most-recent published investment reports for the same
market. Sourced from :class:`InvestmentReportQueryService.list_reports`,
which is read-only by construction.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_reports.query_service import (
    InvestmentReportQueryService,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)

_DEFAULT_RECENT_LIMIT: int = 5


class InvestPageSnapshotCollector:
    """Optional ``invest_page`` collector backed by recent investment_reports."""

    snapshot_kind: str = "invest_page"

    def __init__(
        self,
        session: AsyncSession,
        *,
        query_service: InvestmentReportQueryService | None = None,
        recent_limit: int = _DEFAULT_RECENT_LIMIT,
    ) -> None:
        self._session = session
        self._query = query_service or InvestmentReportQueryService(session)
        self._recent_limit = max(1, recent_limit)

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        try:
            rows = await self._query.list_reports(
                market=request.market,
                status="published",
                limit=self._recent_limit,
            )
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="invest_http",
                    reason=(f"invest_page query failed: {type(exc).__name__}: {exc}"),
                    as_of=now,
                )
            ]

        reports_payload: list[dict[str, Any]] = [
            {
                "report_uuid": row.report_uuid,
                "report_type": row.report_type,
                "status": row.status,
                "title": row.title,
                "published_at": row.published_at,
                "snapshot_bundle_uuid": row.snapshot_bundle_uuid,
                "snapshot_freshness_overall": (
                    (row.snapshot_freshness_summary or {}).get("overall")
                    if row.snapshot_freshness_summary
                    else None
                ),
            }
            for row in rows
        ]
        payload: dict[str, Any] = {
            "market": request.market,
            "count": len(reports_payload),
            "recent_published_reports": reports_payload,
        }
        if not reports_payload:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="invest_http",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"recent_published": 0},
                )
            ]
        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="invest_http",
                as_of=now,
                coverage={"recent_published": len(reports_payload)},
            )
        ]
