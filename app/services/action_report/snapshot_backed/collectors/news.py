"""News snapshot collector (read-only, optional).

Reads recent research reports / news ingestor citations via
:class:`ResearchReportsQueryService`. Optional kind — a soft failure here
degrades the bundle to ``partial`` but never blocks the report.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.action_report.snapshot_backed.collectors._base import (
    build_result,
    unavailable_result,
    utcnow,
)
from app.services.investment_snapshots.collectors import (
    CollectorRequest,
    SnapshotCollectResult,
)
from app.services.research_reports.query_service import ResearchReportsQueryService


class NewsSnapshotCollector:
    """Optional ``news`` collector backed by ``research_reports``."""

    snapshot_kind: str = "news"

    def __init__(
        self,
        session: AsyncSession,
        *,
        query_service: ResearchReportsQueryService | None = None,
        lookback_hours: int = 24,
        limit: int = 20,
    ) -> None:
        self._session = session
        self._query = query_service or ResearchReportsQueryService(session)
        self._lookback_hours = max(1, lookback_hours)
        self._limit = max(1, limit)

    async def collect(self, request: CollectorRequest) -> list[SnapshotCollectResult]:
        now = utcnow()
        since = now - dt.timedelta(hours=self._lookback_hours)

        try:
            response = await self._query.find_relevant(
                since=since,
                limit=self._limit,
            )
        except Exception as exc:  # noqa: BLE001 — optional, fail open
            return [
                unavailable_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    origin="news",
                    reason=f"research_reports query failed: {type(exc).__name__}: {exc}",
                    as_of=now,
                )
            ]

        citations_payload: list[dict[str, Any]] = [
            c.model_dump(mode="json") for c in response.citations
        ]
        payload: dict[str, Any] = {
            "since": since.isoformat(),
            "count": len(citations_payload),
            "citations": citations_payload,
        }

        if not citations_payload:
            return [
                build_result(
                    snapshot_kind=self.snapshot_kind,
                    market=request.market,
                    account_scope=request.account_scope,
                    payload=payload,
                    origin="news",
                    as_of=now,
                    freshness_status="partial",
                    coverage={"citation_count": 0},
                )
            ]

        return [
            build_result(
                snapshot_kind=self.snapshot_kind,
                market=request.market,
                account_scope=request.account_scope,
                payload=payload,
                origin="news",
                as_of=now,
                coverage={"citation_count": len(citations_payload)},
            )
        ]
