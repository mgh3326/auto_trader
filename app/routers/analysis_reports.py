"""ROB-257 analyst report/action-center API routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.analysis_reports import (
    AnalysisCandidateListResponse,
    AnalysisOrderCandidateResponse,
    AnalysisReportCreateRequest,
    AnalysisReportListResponse,
    AnalysisReportResponse,
)
from app.services.analysis_report_service import AnalysisReportService

router = APIRouter(tags=["analysis-reports"])


def get_analysis_report_service(
    db: AsyncSession = Depends(get_db),
) -> AnalysisReportService:
    return AnalysisReportService(db)


@router.post(
    "/trading/api/analysis-reports",
    response_model=AnalysisReportResponse,
    summary="Create a decision-only analysis report artifact",
)
async def create_analysis_report(
    request: AnalysisReportCreateRequest,
    user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[AnalysisReportService, Depends(get_analysis_report_service)],
) -> dict:
    created_by = (
        getattr(user, "username", None) or getattr(user, "email", None) or "analyst"
    )
    return await service.create_report(request, created_by_profile=created_by)


@router.get(
    "/invest/api/action-center/reports",
    response_model=AnalysisReportListResponse,
    summary="List action center analysis report artifacts",
)
@router.get(
    "/trading/api/analysis-reports",
    response_model=AnalysisReportListResponse,
    summary="List analysis report artifacts",
)
async def list_analysis_reports(
    service: Annotated[AnalysisReportService, Depends(get_analysis_report_service)],
    market: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    return await service.list_reports(market=market, status=status_filter, limit=limit)


@router.get(
    "/invest/api/action-center/reports/{report_uuid}",
    response_model=AnalysisReportResponse,
    summary="Get one action center analysis report artifact",
)
@router.get(
    "/trading/api/analysis-reports/{report_uuid}",
    response_model=AnalysisReportResponse,
    summary="Get one analysis report artifact",
)
async def get_analysis_report(
    report_uuid: str,
    service: Annotated[AnalysisReportService, Depends(get_analysis_report_service)],
) -> dict:
    report = await service.get_report(report_uuid)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return report


@router.get(
    "/invest/api/action-center/candidates",
    response_model=AnalysisCandidateListResponse,
    summary="List decision-only action center candidates",
)
async def list_action_center_candidates(
    service: Annotated[AnalysisReportService, Depends(get_analysis_report_service)],
    market: str | None = None,
    symbol: str | None = None,
    approval_status: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> dict:
    return await service.list_candidates(
        market=market,
        symbol=symbol,
        approval_status=approval_status,
        limit=limit,
    )


@router.get(
    "/invest/api/action-center/candidates/{candidate_uuid}",
    response_model=AnalysisOrderCandidateResponse,
    summary="Get one decision-only action center candidate",
)
async def get_action_center_candidate(
    candidate_uuid: str,
    service: Annotated[AnalysisReportService, Depends(get_analysis_report_service)],
) -> dict:
    candidate = await service.get_candidate(candidate_uuid)
    if candidate is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return candidate
