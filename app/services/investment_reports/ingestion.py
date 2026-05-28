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

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.investment_reports import InvestmentReport
from app.schemas.investment_reports import IngestReportItem, IngestReportRequest
from app.services.action_report.common.bundle_aware_publishing import (
    enforce_stale_gate_for_ingest,
)
from app.services.investment_reports.idempotency import item_key, report_key
from app.services.investment_reports.repository import InvestmentReportsRepository


class ReportOverwriteBlockedError(RuntimeError):
    """ROB-352 — raised when an overwrite would destroy operator audit.

    Deleting a report's items cascades to
    ``investment_report_item_decisions`` (ON DELETE CASCADE) and orphans
    activated ``investment_watch_alerts`` (source ref set NULL). When such
    audit exists, overwrite is refused; the caller must supersede/revise via
    a separate path instead.
    """

    def __init__(
        self,
        *,
        report_uuid: object,
        decision_count: int,
        active_alert_count: int,
    ) -> None:
        super().__init__(
            f"overwrite blocked: report {report_uuid} has {decision_count} "
            f"operator decision(s) and {active_alert_count} active watch "
            "alert(s); regenerating would destroy that audit trail"
        )
        self.report_uuid = report_uuid
        self.decision_count = decision_count
        self.active_alert_count = active_alert_count


class InvestmentReportIngestionService:
    """Atomic, idempotent report-bundle creation."""

    def __init__(
        self,
        session: AsyncSession,
        repository: InvestmentReportsRepository | None = None,
    ) -> None:
        self._session = session
        self._repo = repository or InvestmentReportsRepository(session)

    async def get_existing_with_item_count(
        self, request: Any
    ) -> tuple[InvestmentReport, int] | None:
        """ROB-352 — return ``(stored report, item_count)`` for this request's
        idempotency key, or ``None`` when no report exists yet.

        Used by the generator's default-reuse short-circuit so it can build a
        response from the STORED row instead of recomputing a divergent,
        unstored payload. ``request`` is duck-typed: any object carrying the
        seven idempotency-key fields works (both ``IngestReportRequest`` and
        the generator's ``ReportGenerationRequest`` qualify).
        """
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
        if existing is None:
            return None
        items = await self._repo.list_items_for_report(existing.id)
        return existing, len(items)

    async def ingest(
        self,
        request: IngestReportRequest,
        *,
        overwrite: bool = False,
        overwrite_reason: str | None = None,
    ) -> InvestmentReport:
        """Thin wrapper returning only the report (backward-compatible)."""
        report, _reused, _count = await self.ingest_with_outcome(
            request, overwrite=overwrite, overwrite_reason=overwrite_reason
        )
        return report

    async def ingest_with_outcome(
        self,
        request: IngestReportRequest,
        *,
        overwrite: bool = False,
        overwrite_reason: str | None = None,
    ) -> tuple[InvestmentReport, bool, int]:
        """ROB-352 — ingest and report ``(report, reused, item_count)``.

        ``reused`` is True only when an existing row was returned unchanged
        (default path, no overwrite). The generator uses this to rebuild its
        response from the stored row even when a concurrent insert lands
        between its existence precheck and this call — eliminating any
        stored-row/response mismatch on the reuse path.
        """
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
        # ROB-352 — default reuse: return the stored row unchanged. Only an
        # explicit overwrite transactionally replaces it (items + scalar/JSONB
        # fields) while keeping report_uuid / idempotency_key stable. Mutating
        # report_type/created_by_profile to force a new row is NOT supported.
        if existing is not None and not overwrite:
            items = await self._repo.list_items_for_report(existing.id)
            return existing, True, len(items)

        # ROB-269 Phase 3 layer (ii) + (iii) — evaluate gate before insert.
        # When ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED is True and the
        # gate rejects, the helper raises StaleGateRejection and the row is
        # NOT written. When the flag is False the gate is purely advisory
        # — the result is attached to report_metadata under "stale_gate"
        # for audit. Legacy/informational reports bypass both layers (the
        # helper returns a non-rejecting result). The gate applies to both
        # the insert and the overwrite path.
        gate_result = enforce_stale_gate_for_ingest(
            request,
            flag_enabled=settings.ACTION_REPORT_BUNDLE_BASED_GENERATION_ENABLED,
        )
        report_metadata = dict(request.metadata)
        report_metadata.setdefault("stale_gate", gate_result.to_metadata_summary())
        if overwrite and overwrite_reason is not None:
            report_metadata["overwrite_reason"] = overwrite_reason

        # ROB-352 — explicit overwrite: update the existing row in place and
        # replace its items, keeping report_uuid stable.
        if existing is not None:
            # Refuse to clobber operator audit. Deleting items would cascade to
            # decisions and orphan activated watch alerts — block instead.
            existing_items = await self._repo.list_items_for_report(existing.id)
            existing_item_ids = [it.id for it in existing_items]
            decisions = (
                await self._repo.list_decisions_for_items(existing_item_ids)
                if existing_item_ids
                else []
            )
            active_alerts = await self._repo.list_alerts_for_source_reports(
                [existing.report_uuid], status="active"
            )
            if decisions or active_alerts:
                raise ReportOverwriteBlockedError(
                    report_uuid=existing.report_uuid,
                    decision_count=len(decisions),
                    active_alert_count=len(active_alerts),
                )
            await self._repo.update_report(
                existing.id,
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
                report_metadata=report_metadata,
                valid_until=request.valid_until,
                published_at=request.published_at,
                snapshot_bundle_uuid=request.snapshot_bundle_uuid,
                snapshot_policy_version=request.snapshot_policy_version,
                snapshot_coverage_summary=request.snapshot_coverage_summary,
                snapshot_freshness_summary=request.snapshot_freshness_summary,
                source_conflicts=request.source_conflicts,
                unavailable_sources=request.unavailable_sources,
                snapshot_report_diagnostics=request.snapshot_report_diagnostics,
            )
            await self._repo.delete_items_for_report(existing.id)
            for item_req in request.items:
                await self._insert_item(existing, item_req)
            await self._session.flush()
            await self._session.refresh(existing)
            return existing, False, len(request.items)

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
            report_metadata=report_metadata,
            valid_until=request.valid_until,
            published_at=request.published_at,
            # ROB-269 Phase 3 — bundle metadata round-trip. None values are
            # legal (legacy reports). DB CHECK only rejects published rows
            # whose snapshot_freshness_summary['overall'] is stale.
            snapshot_bundle_uuid=request.snapshot_bundle_uuid,
            snapshot_policy_version=request.snapshot_policy_version,
            snapshot_coverage_summary=request.snapshot_coverage_summary,
            snapshot_freshness_summary=request.snapshot_freshness_summary,
            source_conflicts=request.source_conflicts,
            unavailable_sources=request.unavailable_sources,
            # ROB-318 Phase 3 — deterministic report diagnostics bundle.
            snapshot_report_diagnostics=request.snapshot_report_diagnostics,
        )

        for item_req in request.items:
            await self._insert_item(report, item_req)

        await self._session.flush()
        return report, False, len(request.items)

    async def _insert_item(
        self, report: InvestmentReport, item_req: IngestReportItem
    ) -> None:
        watch_condition_payload = (
            item_req.watch_condition.model_dump(mode="json")
            if item_req.watch_condition is not None
            else None
        )
        # ROB-274 — ``target_ref`` is a Pydantic model on the schema side but
        # stored as JSONB. Mirror the watch_condition serialisation pattern
        # (``mode="json"``) so Decimal / datetime / UUID land as JSON-safe
        # primitives. ``current_state`` / ``proposed_state`` / ``diff`` are
        # already plain JSON-safe collections by schema design (the
        # generator normalises Decimals upstream via ``to_jsonable``).
        target_ref_payload = (
            item_req.target_ref.model_dump(mode="json")
            if item_req.target_ref is not None
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
            # ROB-274 proposal-state fields. All optional — legacy callers
            # (operation=None) persist NULL into every new column and the
            # operation-aware CHECKs on the items table let them through.
            operation=item_req.operation,
            target_ref=target_ref_payload,
            current_state=item_req.current_state,
            proposed_state=item_req.proposed_state,
            diff=item_req.diff,
            apply_policy=item_req.apply_policy,
            decision_bucket=item_req.decision_bucket,
            cited_symbol_report_uuid=item_req.cited_symbol_report_uuid,
            cited_dimension_report_uuids=list(item_req.cited_dimension_report_uuids),
        )
