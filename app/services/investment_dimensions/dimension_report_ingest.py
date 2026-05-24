"""Hermes dimension-report ingest service (ROB-306).

Validates + persists Hermes-pushed per-dimension analyst reports (push-only —
never calls an LLM in-process). Mirrors symbol_report_ingest.py, minus verdict
derivation: a dimension report keeps Hermes's ``stance``. auto_trader caps
``confidence`` by the report's freshness status.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.investment_stages import InvestmentStageRun
from app.models.investment_dimension_reports import InvestmentDimensionReport
from app.schemas.investment_dimension_reports import (
    HermesDimensionReport,
    HermesDimensionReportsIngestRequest,
)
from app.services.investment_dimensions.dimension_report_repository import (
    DimensionReportRepository,
)
from app.services.investment_stages.repository import InvestmentStagesRepository

# Reuse the ROB-304 freshness cap policy.
_FRESHNESS_CAP = {"fresh": 100, "partial": 60, "stale": 40, "missing": 20}


class DimensionReportIngestError(RuntimeError):
    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DimensionReportIngestResult:
    dimension: str
    report: InvestmentDimensionReport
    idempotent_existing: bool


@dataclass(frozen=True)
class DimensionReportsIngestResponse:
    run: InvestmentStageRun
    results: list[DimensionReportIngestResult]


def _freshness_status(payload: HermesDimensionReport) -> str:
    fs = payload.freshness_summary or {}
    status = fs.get("status")
    return status if status in _FRESHNESS_CAP else "partial"


def cap_confidence(payload: HermesDimensionReport) -> int | None:
    if payload.confidence is None:
        return None
    cap = _FRESHNESS_CAP.get(_freshness_status(payload), 40)
    return min(payload.confidence, cap)


def content_hash(payload: HermesDimensionReport, *, capped_confidence: int | None) -> str:
    canonical: dict[str, Any] = {
        "dimension": payload.dimension,
        "market": payload.market,
        "symbol": payload.symbol,
        "report_text": payload.report_text,
        "key_findings": payload.key_findings or [],
        "signals": payload.signals or {},
        "stance": payload.stance,
        "confidence": capped_confidence,
        "missing_data": payload.missing_data or [],
        "freshness_summary": payload.freshness_summary or {},
        "cited_snapshot_uuids": sorted(str(u) for u in payload.cited_snapshot_uuids),
    }
    blob = json.dumps(canonical, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class DimensionReportIngestService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        stages_repository: InvestmentStagesRepository | None = None,
        reports_repository: DimensionReportRepository | None = None,
    ) -> None:
        self._session = session
        self._stages = stages_repository or InvestmentStagesRepository(session)
        self._reports = reports_repository or DimensionReportRepository(session)

    async def ingest_from_hermes(
        self, request: HermesDimensionReportsIngestRequest
    ) -> DimensionReportsIngestResponse:
        envelope = request.run_envelope
        run = await self._stages.get_run(envelope.run_uuid)
        if run is None:
            raise DimensionReportIngestError(
                f"stage run not found: {envelope.run_uuid}",
                code="stage_run_not_found",
            )
        if run.snapshot_bundle_uuid != envelope.snapshot_bundle_uuid or (
            run.market != envelope.market
        ):
            raise DimensionReportIngestError(
                f"envelope inconsistent with stage run {envelope.run_uuid}",
                code="run_envelope_mismatch",
            )

        results: list[DimensionReportIngestResult] = []
        for payload in request.dimension_reports:
            report, idem = await self._persist_or_reuse(run=run, payload=payload)
            results.append(
                DimensionReportIngestResult(
                    dimension=payload.dimension, report=report, idempotent_existing=idem
                )
            )
        return DimensionReportsIngestResponse(run=run, results=results)

    async def _persist_or_reuse(
        self, *, run: InvestmentStageRun, payload: HermesDimensionReport
    ) -> tuple[InvestmentDimensionReport, bool]:
        capped = cap_confidence(payload)
        digest = content_hash(payload, capped_confidence=capped)
        key = (
            f"{run.run_uuid}:{payload.dimension}:{payload.market}:"
            f"{payload.symbol or ''}:{digest}"
        )
        existing = await self._reports.get_by_idempotency_key(key)
        if existing is not None:
            return existing, True

        version = await self._reports.next_version(
            run_uuid=run.run_uuid, dimension=payload.dimension,
            market=payload.market, symbol=payload.symbol,
        )
        report = await self._reports.persist(
            run_uuid=run.run_uuid,
            snapshot_bundle_uuid=run.snapshot_bundle_uuid,
            dimension=payload.dimension,
            market=payload.market,
            account_scope=run.account_scope,
            symbol=payload.symbol,
            artifact_version=version,
            report_text=payload.report_text,
            key_findings=payload.key_findings,
            signals=payload.signals,
            stance=payload.stance,
            confidence=capped,
            missing_data=payload.missing_data,
            freshness_summary=payload.freshness_summary,
            content_hash=digest,
            cited_snapshot_uuids=list(payload.cited_snapshot_uuids),
            idempotency_key=key,
        )
        return report, False
