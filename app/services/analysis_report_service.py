"""Service layer for ROB-257 analyst report decision artifacts.

This service persists analyst/research report outputs and manual-approval
candidates only. It does not call broker, order, watch, or notification clients.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import (
    AnalysisOrderCandidate,
    AnalysisReport,
    AnalysisStageResult,
)
from app.schemas.analysis_reports import AnalysisReportCreateRequest


class AnalysisReportService:
    """Only write/read path for ROB-257 analysis report artifacts."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def create_report(
        self, request: AnalysisReportCreateRequest, *, created_by_profile: str
    ) -> dict[str, Any]:
        existing = await self._get_report_by_idempotency(request.idempotency_key)
        if existing is not None:
            serialized = await self._serialize_report(existing)
            serialized["idempotent"] = True
            return serialized

        report = AnalysisReport(
            idempotency_key=request.idempotency_key,
            report_type=request.report_type,
            market=request.market,
            account_scope=request.account_scope,
            created_by_profile=created_by_profile,
            status=request.status,
            summary=request.summary,
            risk_summary=request.risk_summary,
            data_freshness=request.data_freshness,
            coverage=request.coverage,
            source_policy=request.source_policy,
            safety_notes=request.safety_notes,
            report_metadata=request.metadata,
            published_at=request.published_at,
            valid_until=request.valid_until,
        )
        self.db.add(report)
        await self.db.flush()

        for stage in request.stage_results:
            self.db.add(
                AnalysisStageResult(
                    report_id=report.id,
                    stage_key=stage.stage_key,
                    source=stage.source,
                    provenance=stage.provenance,
                    status=stage.status,
                    freshness_at=stage.freshness_at,
                    raw_payload=stage.raw_payload,
                    normalized_payload=stage.normalized_payload,
                    unavailable_reason=stage.unavailable_reason,
                    warnings=stage.warnings,
                )
            )

        for candidate in request.candidates:
            self.db.add(
                AnalysisOrderCandidate(
                    report_id=report.id,
                    idempotency_key=candidate.idempotency_key,
                    symbol=candidate.symbol,
                    market=candidate.market,
                    side=candidate.side,
                    action_type=candidate.action_type,
                    quantity=candidate.quantity,
                    quantity_pct=candidate.quantity_pct,
                    limit_price=candidate.limit_price,
                    notional=candidate.notional,
                    currency=candidate.currency,
                    priority=candidate.priority,
                    confidence=candidate.confidence,
                    thesis=candidate.thesis,
                    risk_notes=candidate.risk_notes,
                    verification=candidate.verification,
                    blocking_reasons=candidate.blocking_reasons,
                    approval_status=candidate.approval_status,
                    approval_type=candidate.approval_type,
                    policy_id=candidate.policy_id,
                    policy_snapshot=candidate.policy_snapshot,
                    execution_state=candidate.execution_state,
                    valid_until=candidate.valid_until,
                )
            )

        await self.db.commit()
        await self.db.refresh(report)
        serialized = await self._serialize_report(report)
        serialized["idempotent"] = False
        return serialized

    async def list_reports(
        self,
        *,
        market: str | None = None,
        status: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        stmt = (
            select(AnalysisReport)
            .order_by(AnalysisReport.created_at.desc())
            .limit(limit)
        )
        if market:
            stmt = stmt.where(AnalysisReport.market == market)
        if status:
            stmt = stmt.where(AnalysisReport.status == status)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [
            await self._serialize_report(row, include_children=False) for row in rows
        ]
        return {"count": len(items), "items": items}

    async def get_report(self, report_uuid: str) -> dict[str, Any] | None:
        stmt = select(AnalysisReport).where(
            AnalysisReport.report_uuid == _coerce_uuid(report_uuid)
        )
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return await self._serialize_report(row)

    async def list_candidates(
        self,
        *,
        market: str | None = None,
        symbol: str | None = None,
        approval_status: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 100))
        stmt = (
            select(AnalysisOrderCandidate)
            .order_by(
                AnalysisOrderCandidate.priority.desc(),
                AnalysisOrderCandidate.created_at.desc(),
            )
            .limit(limit)
        )
        if market:
            stmt = stmt.where(AnalysisOrderCandidate.market == market)
        if symbol:
            stmt = stmt.where(AnalysisOrderCandidate.symbol == symbol)
        if approval_status:
            stmt = stmt.where(AnalysisOrderCandidate.approval_status == approval_status)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [await self._serialize_candidate(row) for row in rows]
        return {"count": len(items), "items": items}

    async def get_candidate(self, candidate_uuid: str) -> dict[str, Any] | None:
        stmt = select(AnalysisOrderCandidate).where(
            AnalysisOrderCandidate.candidate_uuid == _coerce_uuid(candidate_uuid)
        )
        row = (await self.db.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return await self._serialize_candidate(row)

    async def _get_report_by_idempotency(self, key: str) -> AnalysisReport | None:
        stmt = select(AnalysisReport).where(AnalysisReport.idempotency_key == key)
        return (await self.db.execute(stmt)).scalar_one_or_none()

    async def _serialize_report(
        self, report: AnalysisReport, *, include_children: bool = True
    ) -> dict[str, Any]:
        stages: list[dict[str, Any]] = []
        candidates: list[dict[str, Any]] = []
        if include_children:
            stage_rows = (
                (
                    await self.db.execute(
                        select(AnalysisStageResult)
                        .where(AnalysisStageResult.report_id == report.id)
                        .order_by(AnalysisStageResult.id.asc())
                    )
                )
                .scalars()
                .all()
            )
            stages = [_serialize_stage(row) for row in stage_rows]
            candidate_rows = (
                (
                    await self.db.execute(
                        select(AnalysisOrderCandidate)
                        .where(AnalysisOrderCandidate.report_id == report.id)
                        .order_by(
                            AnalysisOrderCandidate.priority.desc(),
                            AnalysisOrderCandidate.id.asc(),
                        )
                    )
                )
                .scalars()
                .all()
            )
            candidates = [
                await self._serialize_candidate(row, report=report)
                for row in candidate_rows
            ]
        return {
            "id": report.id,
            "report_uuid": str(report.report_uuid),
            "idempotency_key": report.idempotency_key,
            "report_type": report.report_type,
            "market": report.market,
            "account_scope": report.account_scope,
            "created_by_profile": report.created_by_profile,
            "status": report.status,
            "summary": report.summary,
            "risk_summary": report.risk_summary,
            "data_freshness": report.data_freshness,
            "coverage": report.coverage,
            "source_policy": report.source_policy,
            "safety_notes": report.safety_notes,
            "metadata": report.report_metadata,
            "created_at": report.created_at,
            "published_at": report.published_at,
            "valid_until": report.valid_until,
            "stages": stages,
            "candidates": candidates,
        }

    async def _serialize_candidate(
        self, candidate: AnalysisOrderCandidate, *, report: AnalysisReport | None = None
    ) -> dict[str, Any]:
        report_uuid = None
        if report is not None:
            report_uuid = str(report.report_uuid)
        elif candidate.report_id is not None:
            stmt = select(AnalysisReport.report_uuid).where(
                AnalysisReport.id == candidate.report_id
            )
            report_uuid_value = (await self.db.execute(stmt)).scalar_one_or_none()
            report_uuid = (
                str(report_uuid_value) if report_uuid_value is not None else None
            )
        return _serialize_candidate(candidate, report_uuid=report_uuid)


def _serialize_stage(stage: AnalysisStageResult) -> dict[str, Any]:
    return {
        "id": stage.id,
        "stage_key": stage.stage_key,
        "source": stage.source,
        "provenance": stage.provenance,
        "status": stage.status,
        "freshness_at": stage.freshness_at,
        "raw_payload": stage.raw_payload,
        "normalized_payload": stage.normalized_payload,
        "unavailable_reason": stage.unavailable_reason,
        "warnings": stage.warnings,
        "created_at": stage.created_at,
    }


def _serialize_candidate(
    candidate: AnalysisOrderCandidate, *, report_uuid: str | None
) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "candidate_uuid": str(candidate.candidate_uuid),
        "report_uuid": report_uuid,
        "idempotency_key": candidate.idempotency_key,
        "symbol": candidate.symbol,
        "market": candidate.market,
        "side": candidate.side,
        "action_type": candidate.action_type,
        "quantity": _decimal_or_none(candidate.quantity),
        "quantity_pct": _decimal_or_none(candidate.quantity_pct),
        "limit_price": _decimal_or_none(candidate.limit_price),
        "notional": _decimal_or_none(candidate.notional),
        "currency": candidate.currency,
        "priority": candidate.priority,
        "confidence": _decimal_or_none(candidate.confidence),
        "thesis": candidate.thesis,
        "risk_notes": candidate.risk_notes,
        "verification": candidate.verification,
        "blocking_reasons": candidate.blocking_reasons,
        "approval_status": candidate.approval_status,
        "approval_type": candidate.approval_type,
        "approved_by": candidate.approved_by,
        "approved_at": candidate.approved_at,
        "rejected_by": candidate.rejected_by,
        "rejected_at": candidate.rejected_at,
        "policy_id": candidate.policy_id,
        "policy_snapshot": candidate.policy_snapshot,
        "execution_state": candidate.execution_state,
        "linked_trade_journal_id": candidate.linked_trade_journal_id,
        "linked_order_ledger_ref": candidate.linked_order_ledger_ref,
        "created_at": candidate.created_at,
        "valid_until": candidate.valid_until,
    }


def _decimal_or_none(value: Any) -> Decimal | None:
    return None if value is None else Decimal(str(value))


def _coerce_uuid(value: str) -> UUID:
    return UUID(str(value))
