"""Read-only research reports router (ROB-140).

GET only. No mutation. Auth required (matches existing trading/api pattern).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.research_reports import ResearchReportCitationListResponse
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
    since: Annotated[datetime | None, Query(description="ISO8601 inclusive lower bound on published_at")] = None,
    until: Annotated[datetime | None, Query(description="ISO8601 inclusive upper bound on published_at")] = None,
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
