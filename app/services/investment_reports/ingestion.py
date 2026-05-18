"""ROB-265 — Idempotent investment-report ingestion service.

Takes an :class:`IngestReportRequest`, returns the persisted
:class:`InvestmentReport`. Idempotent on the report's composed
idempotency key: a second call with the same
``(report_type, market, market_session, kst_date, generator_version)``
returns the existing report unchanged. Items are NOT re-applied or
diff-merged on re-ingest — the report bundle is atomic by design.

Service-level only. No broker mutation, no MCP wiring, no scanner side
effects. Callers own the transaction boundary (this service flushes
but never commits).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_reports import InvestmentReport
from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.investment_reports.idempotency import item_key, report_key
from app.services.investment_reports.repository import InvestmentReportsRepository


class InvestmentReportIngestionService:
    """Atomic, idempotent report-bundle creation."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def ingest(self, request: IngestReportRequest) -> InvestmentReport:
        idempotency_key = report_key(
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            kst_date=request.kst_date,
            generator_version=request.generator_version,
        )

        existing = await self._repo.get_report_by_idempotency_key(idempotency_key)
        if existing is not None:
            return existing

        report = await self._repo.insert_report(
            idempotency_key=idempotency_key,
            report_type=request.report_type,
            market=request.market,
            market_session=request.market_session,
            account_scope=request.account_scope,
            execution_mode=request.execution_mode,
            created_by_profile=request.created_by_profile,
            title=request.title,
            summary=request.summary,
            risk_summary=request.risk_summary,
            thesis_text=request.thesis_text,
            no_action_note=request.no_action_note,
            market_snapshot=request.market_snapshot,
            portfolio_snapshot=request.portfolio_snapshot,
            previous_report_uuid=request.previous_report_uuid,
            status=request.status,
            report_metadata=request.metadata,
            valid_until=request.valid_until,
            published_at=request.published_at,
        )

        for item_req in request.items:
            await self._insert_item(report, item_req)

        await self._session.flush()
        return report

    async def _insert_item(
        self, report: InvestmentReport, item_req: IngestReportItem
    ) -> None:
        watch_condition_payload = (
            item_req.watch_condition.model_dump(mode="json")
            if item_req.watch_condition is not None
            else None
        )
        idempotency_key = item_key(
            report_uuid=str(report.report_uuid),
            client_item_key=item_req.client_item_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            watch_condition=watch_condition_payload,
        )
        await self._repo.insert_item(
            report_id=report.id,
            idempotency_key=idempotency_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            target_kind=item_req.target_kind,
            priority=item_req.priority,
            confidence=item_req.confidence,
            rationale=item_req.rationale,
            evidence_snapshot=item_req.evidence_snapshot,
            watch_condition=watch_condition_payload,
            trigger_checklist=item_req.trigger_checklist,
            max_action=item_req.max_action,
            valid_until=item_req.valid_until,
            item_metadata=item_req.metadata,
        )
