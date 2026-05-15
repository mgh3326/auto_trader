"""Research reports router (ROB-140/ROB-207).

GET endpoints are user-authenticated. Bulk POST ingest uses token auth and is
reserved for approval-gated ingestion bridge writes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.research_reports import (
    ResearchReportCitationListResponse,
    ResearchReportIngestionRequest,
    ResearchReportIngestionResponse,
    ResearchReportsReadinessResponse,
)
from app.services.research_reports.freshness import compute_research_reports_readiness
from app.services.research_reports.ingestion import ingest_research_reports_v1
from app.services.research_reports.query_service import (
    ResearchReportsQueryService,
)

router = APIRouter(prefix="/trading", tags=["research-reports"])


@router.get(
    "/api/research-reports/recent",
    response_model=ResearchReportCitationListResponse,
)
async def get_recent_research_reports(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    symbol: str | None = None,
    query: str | None = None,
    source: str | None = None,
    since: Annotated[
        datetime | None,
        Query(description="ISO8601 inclusive lower bound on published_at"),
    ] = None,
    until: Annotated[
        datetime | None,
        Query(description="ISO8601 inclusive upper bound on published_at"),
    ] = None,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> ResearchReportCitationListResponse:
    svc = ResearchReportsQueryService(db)
    try:
        return await svc.find_relevant(
            symbol=symbol,
            query=query,
            source=source,
            since=since,
            until=until,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post(
    "/api/research-reports/ingest/bulk",
    response_model=ResearchReportIngestionResponse,
)
async def bulk_ingest_research_reports(
    request: ResearchReportIngestionRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ResearchReportIngestionResponse:
    # Auth: validated upstream by AuthMiddleware against
    # settings.RESEARCH_REPORTS_INGEST_TOKEN. Do NOT require session user here.
    try:
        result = await ingest_research_reports_v1(db, request)
        await db.commit()
        return result
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get(
    "/api/research-reports/freshness",
    response_model=ResearchReportsReadinessResponse,
)
async def get_research_reports_freshness(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    source: str | None = None,
    max_age_hours: Annotated[int | None, Query(ge=1, le=168)] = None,
) -> ResearchReportsReadinessResponse:
    from app.core.config import settings

    budget = max_age_hours or settings.RESEARCH_REPORTS_FRESHNESS_MAX_AGE_HOURS
    return await compute_research_reports_readiness(
        db,
        source=source,
        max_age_hours=budget,
    )
