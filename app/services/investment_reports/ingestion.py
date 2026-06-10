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
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.investment_reports import InvestmentReport, InvestmentReportItem
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


class DraftReportMutationBlockedError(RuntimeError):
    """Raised when a draft-only mutation targets a non-draft report."""

    def __init__(self, *, report_uuid: object, status: str) -> None:
        super().__init__(
            f"draft mutation blocked: report {report_uuid} has status {status!r}"
        )
        self.report_uuid = report_uuid
        self.status = status


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

    async def find_existing_report(
        self,
        *,
        report_type: str,
        market: str,
        market_session: str | None,
        account_scope: str | None,
        execution_mode: str,
        kst_date: str,
        generator_version: str,
    ) -> InvestmentReport | None:
        """ROB-380 — resolve the report for this idempotency identity, or None.

        Lets a caller (the mock_preview runner) learn whether
        :meth:`ingest_with_outcome` will idempotently return an existing row
        BEFORE it builds a snapshot bundle — so it never creates a bundle that
        the idempotent-reuse return would orphan. Reuses the same ``report_key``
        composition as the ingest path (no drift).
        """
        idempotency_key = report_key(
            report_type=report_type,
            market=market,
            market_session=market_session,
            account_scope=account_scope,
            execution_mode=execution_mode,
            kst_date=kst_date,
            generator_version=generator_version,
        )
        return await self._repo.get_report_by_idempotency_key(idempotency_key)

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
            # ROB-455 — overwrite that (re)sets a predecessor chain supersedes it.
            await self._maybe_supersede_previous(request.previous_report_uuid, existing)
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
        # ROB-455 — a new report that chains from a predecessor supersedes it.
        await self._maybe_supersede_previous(request.previous_report_uuid, report)
        return report, False, len(request.items)

    async def set_report_status(
        self,
        *,
        report_uuid: UUID,
        status: str,
        reason: str | None = None,
        actor: str | None = None,
        superseded_by: UUID | None = None,
    ) -> InvestmentReport | None:
        """ROB-455 — transition a report's lifecycle ``status`` first-class.

        Returns the (refreshed) report, or ``None`` when no report matches
        ``report_uuid`` so the caller can surface ``not_found``. Setting the
        status it already has is an idempotent no-op. Each transition appends a
        ``status_transitions`` entry to ``report_metadata`` for traceability;
        the chosen 'superseded' target also records ``superseded_by``. The DB
        CHECK is the authoritative gate on which status values are legal.
        """
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None
        if report.status == status:
            return report
        metadata = dict(report.report_metadata or {})
        entry: dict[str, Any] = {"to": status}
        if reason is not None:
            entry["reason"] = reason
        if actor is not None:
            entry["actor"] = actor
        if superseded_by is not None:
            metadata["superseded_by"] = str(superseded_by)
            entry["superseded_by"] = str(superseded_by)
        transitions = list(metadata.get("status_transitions") or [])
        transitions.append(entry)
        metadata["status_transitions"] = transitions
        await self._repo.update_report(
            report.id, status=status, report_metadata=metadata
        )
        await self._session.refresh(report)
        return report

    async def add_items_to_draft(
        self, *, report_uuid: UUID, items: list[IngestReportItem]
    ) -> tuple[
        InvestmentReport | None,
        list[InvestmentReportItem],
        list[InvestmentReportItem],
    ]:
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None, [], []
        if report.status != "draft":
            raise DraftReportMutationBlockedError(
                report_uuid=report.report_uuid, status=report.status
            )

        inserted: list[InvestmentReportItem] = []
        existing: list[InvestmentReportItem] = []
        for item_req in items:
            by_client_key = await self._repo.find_item_by_report_client_key(
                report.id, item_req.client_item_key
            )
            if by_client_key is not None:
                existing.append(by_client_key)
                continue

            item_idempotency_key = self._item_idempotency_key(report, item_req)
            by_exact_key = await self._repo.get_item_by_idempotency_key(
                item_idempotency_key
            )
            if by_exact_key is not None:
                existing.append(by_exact_key)
                continue

            inserted.append(await self._insert_item(report, item_req))

        await self._session.flush()
        await self._session.refresh(report)
        return report, inserted, existing

    async def update_draft_report(
        self,
        *,
        report_uuid: UUID,
        updates: dict[str, Any],
        actor: str | None = None,
        reason: str | None = None,
    ) -> InvestmentReport | None:
        report = await self._repo.get_report_by_uuid(report_uuid)
        if report is None:
            return None
        if report.status != "draft":
            raise DraftReportMutationBlockedError(
                report_uuid=report.report_uuid, status=report.status
            )

        allowed = {
            "title",
            "summary",
            "risk_summary",
            "thesis_text",
            "no_action_note",
            "market_snapshot",
            "portfolio_snapshot",
            "valid_until",
        }
        fields = {k: v for k, v in updates.items() if k in allowed}
        metadata = dict(report.report_metadata or {})
        metadata_patch = updates.get("metadata")
        if isinstance(metadata_patch, dict):
            metadata.update(metadata_patch)

        audit_entry: dict[str, Any] = {"fields": sorted(updates.keys())}
        if actor is not None:
            audit_entry["actor"] = actor
        if reason is not None:
            audit_entry["reason"] = reason
        draft_updates = list(metadata.get("draft_updates") or [])
        draft_updates.append(audit_entry)
        metadata["draft_updates"] = draft_updates
        fields["report_metadata"] = metadata

        await self._repo.update_report(report.id, **fields)
        await self._session.refresh(report)
        return report

    async def _maybe_supersede_previous(
        self, previous_report_uuid: UUID | None, new_report: InvestmentReport
    ) -> None:
        """ROB-455 — make ``previous_report_uuid`` load-bearing: when a new report
        chains from a predecessor, mark the predecessor 'superseded'.

        No-op when the link is unset or dangles (a trace hint may not resolve),
        and only draft/published predecessors are touched — already-terminal
        (decided/expired/superseded) reports are left as-is.
        """
        if previous_report_uuid is None:
            return
        predecessor = await self._repo.get_report_by_uuid(previous_report_uuid)
        if predecessor is None or predecessor.status not in ("draft", "published"):
            return
        await self.set_report_status(
            report_uuid=previous_report_uuid,
            status="superseded",
            reason="auto_superseded_by_chain",
            superseded_by=new_report.report_uuid,
        )

    @staticmethod
    def _item_idempotency_key(
        report: InvestmentReport, item_req: IngestReportItem
    ) -> str:
        watch_condition_payload = (
            item_req.watch_condition.model_dump(mode="json")
            if item_req.watch_condition is not None
            else None
        )
        return item_key(
            report_uuid=str(report.report_uuid),
            client_item_key=item_req.client_item_key,
            item_kind=item_req.item_kind,
            symbol=item_req.symbol,
            side=item_req.side,
            intent=item_req.intent,
            watch_condition=watch_condition_payload,
        )

    async def _insert_item(
        self, report: InvestmentReport, item_req: IngestReportItem
    ) -> InvestmentReportItem:
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
        idempotency_key = self._item_idempotency_key(report, item_req)
        # ROB-459 P1 — merge typed evidence into the existing evidence_snapshot
        # JSONB under reserved keys (no migration). Round-trips via
        # InvestmentReportItemResponse.evidence_snapshot. When evidence/freshness
        # are unset the keys are NOT added (legacy shape unchanged).
        evidence_payload = dict(item_req.evidence_snapshot or {})
        if item_req.evidence:
            evidence_payload["structured_evidence"] = [
                e.model_dump(mode="json") for e in item_req.evidence
            ]
        if item_req.freshness is not None:
            evidence_payload["item_freshness"] = item_req.freshness
        if item_req.entry_plan:
            evidence_payload["entry_plan"] = [
                level.model_dump(mode="json", exclude_none=True)
                for level in item_req.entry_plan
            ]
        if item_req.stop_loss is not None:
            evidence_payload["stop_loss"] = item_req.stop_loss.model_dump(
                mode="json", exclude_none=True
            )
        if item_req.target_price is not None:
            evidence_payload["target_price"] = item_req.target_price.model_dump(
                mode="json", exclude_none=True
            )
        if item_req.linked_order_ids:
            evidence_payload["linked_order_ids"] = [
                ref.model_dump(mode="json", exclude_none=True)
                for ref in item_req.linked_order_ids
            ]
        item_metadata = dict(item_req.metadata or {})
        item_metadata["client_item_key"] = item_req.client_item_key
        return await self._repo.insert_item(
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
            evidence_snapshot=evidence_payload,
            watch_condition=watch_condition_payload,
            trigger_checklist=item_req.trigger_checklist,
            max_action=item_req.max_action,
            valid_until=item_req.valid_until,
            item_metadata=item_metadata,
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
            cited_snapshot_uuids=list(item_req.cited_snapshot_uuids),
        )
