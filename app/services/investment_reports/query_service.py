"""ROB-265 — Read-only query service over the investment_* tables.

Wraps the repository with the higher-level read shapes the next layers
(MCP/API in Plan 3, frontend in Plan 5) need. ``get_bundle`` returns a
single report with all its nested context. ``previous_report_context``
implements locked refinement #7 — context retrieval is a *query* over
prior reports, not a single-link traversal via ``previous_report_uuid``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import (
    InvestmentReport,
    InvestmentReportItem,
    InvestmentReportItemDecision,
    InvestmentWatchAlert,
    InvestmentWatchEvent,
)
from app.services.investment_reports.repository import InvestmentReportsRepository


class InvestmentReportQueryService:
    """Read-only queries — list / get / latest / previous-context."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def list_reports(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
        limit: int = 20,
    ) -> list[InvestmentReport]:
        return await self._repo.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
            limit=limit,
        )

    async def latest_report(
        self,
        *,
        market: str | None = None,
        market_session: str | None = None,
        account_scope: str | None = None,
        status: str | None = None,
        report_type: str | None = None,
    ) -> InvestmentReport | None:
        return await self._repo.latest_report(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            status=status,
            report_type=report_type,
        )

    async def get_bundle(self, report_uuid: UUID) -> dict[str, Any] | None:
        """Return the report + nested items, decisions, alerts, recent events.

        Returns ``None`` if the report doesn't exist.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None

        items = await self._repo.list_items_for_report(report.id)
        item_ids = [it.id for it in items]
        decisions = await self._repo.list_decisions_for_items(item_ids)
        alerts = await self._repo.list_alerts_for_source_reports([report.report_uuid])
        events = await self._repo.list_events_for_source_reports([report.report_uuid])

        decisions_by_item: dict[int, list[InvestmentReportItemDecision]] = {
            it.id: [] for it in items
        }
        for d in decisions:
            decisions_by_item.setdefault(d.item_id, []).append(d)

        return {
            "report": report,
            "items": items,
            "decisions_by_item": decisions_by_item,
            "alerts": alerts,
            "events": events,
        }

    async def previous_report_context(
        self,
        *,
        market: str,
        market_session: str | None = None,
        account_scope: str | None = None,
        report_type: str | None = None,
        exclude_report_uuid: UUID | None = None,
        n_prior: int = 3,
        events_since: datetime | None = None,
    ) -> dict[str, Any]:
        """Locked refinement #7 — previous context is a query, not a single FK.

        Returns the most recent N prior reports matching the filters plus
        the unresolved/deferred items, active watches, triggered watch
        events, and recent decisions that span those reports.
        """
        prior_reports: list[InvestmentReport] = await self._repo.list_reports(
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            report_type=report_type,
            limit=n_prior + 1,  # +1 so we can drop the excluded one cleanly
        )
        if exclude_report_uuid is not None:
            prior_reports = [
                r for r in prior_reports if r.report_uuid != exclude_report_uuid
            ]
        prior_reports = prior_reports[:n_prior]

        prior_report_uuids = [r.report_uuid for r in prior_reports]
        prior_report_ids = [r.id for r in prior_reports]

        # Items: deferred-status only, across the prior report set.
        unresolved_deferred_items: list[InvestmentReportItem] = []
        for r in prior_reports:
            r_items = await self._repo.list_items_for_report(r.id)
            unresolved_deferred_items.extend(
                it for it in r_items if it.status == "deferred"
            )

        # Active watches sourced from those reports.
        active_watches: list[
            InvestmentWatchAlert
        ] = await self._repo.list_alerts_for_source_reports(
            prior_report_uuids, status="active"
        )

        # Recent triggered events linked to those source reports.
        triggered_events: list[
            InvestmentWatchEvent
        ] = await self._repo.list_events_for_source_reports(
            prior_report_uuids, since=events_since
        )

        # Recent decisions on items in those reports.
        all_item_ids: list[int] = []
        for r in prior_reports:
            r_items = await self._repo.list_items_for_report(r.id)
            all_item_ids.extend(it.id for it in r_items)
        recent_decisions: list[
            InvestmentReportItemDecision
        ] = await self._repo.list_decisions_for_items(all_item_ids)

        # `prior_report_ids` is consumed indirectly above; keep it in the
        # return shape for callers that want the integer IDs.
        return {
            "prior_reports": prior_reports,
            "prior_report_ids": prior_report_ids,
            "unresolved_deferred_items": unresolved_deferred_items,
            "active_watches": active_watches,
            "triggered_events": triggered_events,
            "recent_decisions": recent_decisions,
        }
