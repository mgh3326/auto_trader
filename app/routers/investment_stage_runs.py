"""ROB-279 — Read-only API surface for staged snapshot-backed reports.

Two endpoints:

* ``GET /trading/api/investment-stage-runs/{run_uuid}`` — run-scoped
  diagnostic; returns the stage run row + all artifacts. Useful for
  forensics when the stale gate blocks final report creation.

* ``GET /trading/api/investment-reports/{report_uuid}/stage-artifacts`` —
  report-scoped; returns the union of all artifacts from stage runs
  linked to the report.  Resolution path:

  1. Fetch the InvestmentReport row.
  2. If ``report.report_metadata`` carries ``investment_stage_run_uuid``,
     use that single run UUID.
  3. Else fall back to ``StageRunQueryService.list_runs_for_bundle`` and
     union all artifacts (legacy fallback for reports without explicit
     linkage).
"""

from __future__ import annotations

import uuid as _uuid
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.services.investment_reports.repository import InvestmentReportsRepository
from app.services.investment_stages.query_service import StageRunQueryService

router = APIRouter(tags=["investment-stage-runs"])


# ---------------------------------------------------------------------------
# Pydantic response schemas (inlined — no separate schema file needed yet)
# ---------------------------------------------------------------------------


class StageRunResponse(BaseModel):
    run_uuid: UUID
    snapshot_bundle_uuid: UUID
    market: str
    market_session: str | None
    account_scope: str | None
    policy_version: str
    generator_version: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    metadata_json: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StageArtifactResponse(BaseModel):
    artifact_uuid: UUID
    run_uuid: UUID
    stage_type: str
    verdict: str
    confidence: int
    summary: str | None
    key_points: list[Any] | None
    buy_evidence: list[Any] | None
    sell_evidence: list[Any] | None
    risk_evidence: list[Any] | None
    missing_data: list[Any] | None
    cited_snapshot_uuids: list[UUID]
    freshness_summary: dict[str, Any] | None
    model_name: str | None
    prompt_version: str | None
    payload_hash: str | None
    raw_payload_json: dict[str, Any] | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StageRunWithArtifactsResponse(BaseModel):
    run: StageRunResponse
    artifacts: list[StageArtifactResponse]


class ReportStageArtifactsResponse(BaseModel):
    report_uuid: UUID
    stage_run_uuid: UUID | None
    artifacts: list[StageArtifactResponse]


# ---------------------------------------------------------------------------
# Dependency builders
# ---------------------------------------------------------------------------
def _build_stage_query_service(
    db: AsyncSession = Depends(get_db),
) -> StageRunQueryService:
    return StageRunQueryService(db)


def _build_reports_repository(
    db: AsyncSession = Depends(get_db),
) -> InvestmentReportsRepository:
    return InvestmentReportsRepository(db)


# ---------------------------------------------------------------------------
# Endpoint 1 — run-scoped diagnostic
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-stage-runs/{run_uuid}",
    response_model=StageRunWithArtifactsResponse,
    summary="Get stage run + artifacts by run UUID (ROB-279)",
)
async def get_stage_run(
    run_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    stage_svc: Annotated[StageRunQueryService, Depends(_build_stage_query_service)],
) -> StageRunWithArtifactsResponse:
    result = await stage_svc.get_run_with_artifacts(run_uuid)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="stage_run_not_found"
        )
    return StageRunWithArtifactsResponse(
        run=StageRunResponse.model_validate(result.run),
        artifacts=[StageArtifactResponse.model_validate(a) for a in result.artifacts],
    )


# ---------------------------------------------------------------------------
# Endpoint 2 — report-scoped artifact union
# ---------------------------------------------------------------------------
@router.get(
    "/trading/api/investment-reports/{report_uuid}/stage-artifacts",
    response_model=ReportStageArtifactsResponse,
    summary="Get stage artifacts linked to an investment report (ROB-279)",
)
async def get_report_stage_artifacts(
    report_uuid: UUID,
    _user: Annotated[User, Depends(get_authenticated_user)],
    reports_repo: Annotated[
        InvestmentReportsRepository, Depends(_build_reports_repository)
    ],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ReportStageArtifactsResponse:
    report = await reports_repo.get_report_by_uuid(report_uuid)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="report_not_found"
        )

    stage_svc = StageRunQueryService(db)

    # Resolution path: prefer explicit linkage in report metadata; fall back
    # to bundle-scoped enumeration for legacy reports.
    explicit_run_uuid: UUID | None = None
    raw = (report.report_metadata or {}).get("investment_stage_run_uuid")
    if isinstance(raw, str):
        try:
            explicit_run_uuid = _uuid.UUID(raw)
        except ValueError:
            pass

    # 404 when neither resolution path is available.
    if explicit_run_uuid is None and report.snapshot_bundle_uuid is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="report has neither stage_run_uuid in metadata nor snapshot_bundle_uuid",
        )

    artifacts: list[StageArtifactResponse] = []

    if explicit_run_uuid is not None:
        result = await stage_svc.get_run_with_artifacts(explicit_run_uuid)
        if result is None:
            raise HTTPException(
                status_code=404,
                detail="stage_run_uuid in report_metadata points to a non-existent stage run",
            )
        artifacts = [StageArtifactResponse.model_validate(a) for a in result.artifacts]
    elif report.snapshot_bundle_uuid is not None:
        # Legacy fallback: union artifacts from all runs for this bundle.
        runs = await stage_svc.list_runs_for_bundle(report.snapshot_bundle_uuid)
        for run in runs:
            run_result = await stage_svc.get_run_with_artifacts(run.run_uuid)
            if run_result is not None:
                artifacts.extend(
                    StageArtifactResponse.model_validate(a)
                    for a in run_result.artifacts
                )

    return ReportStageArtifactsResponse(
        report_uuid=report_uuid,
        stage_run_uuid=explicit_run_uuid,
        artifacts=artifacts,
    )
