"""ROB-121 — Research retrospective router (read-only)."""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import get_current_user
from app.core.db import get_db
from app.models.trading import User
from app.schemas.research_retrospective import (
    RetrospectiveDecisionsResponse,
    RetrospectiveOverview,
    StagePerformanceRow,
)
from app.services.research_retrospective_service import (
    ResearchRetrospectiveService,
)

api_router = APIRouter(
    prefix="/api/research-retrospective", tags=["research-retrospective"]
)
router = APIRouter()


def _service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ResearchRetrospectiveService:
    return ResearchRetrospectiveService(db)


def _resolve_window(days: int) -> tuple[datetime, datetime]:
    end = datetime.now(UTC)
    start = end - timedelta(days=days)
    return start, end


@api_router.get("/overview", response_model=RetrospectiveOverview)
async def get_overview(
    user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[ResearchRetrospectiveService, Depends(_service)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Annotated[Literal["KR", "US", "CRYPTO"] | None, Query()] = None,
    strategy: Annotated[str | None, Query(max_length=100)] = None,
) -> RetrospectiveOverview:
    start, end = _resolve_window(days)
    return await svc.build_overview(
        period_start=start, period_end=end, market=market, strategy=strategy
    )


@api_router.get("/stage-performance", response_model=list[StagePerformanceRow])
async def get_stage_performance(
    user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[ResearchRetrospectiveService, Depends(_service)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Annotated[Literal["KR", "US", "CRYPTO"] | None, Query()] = None,
    strategy: Annotated[str | None, Query(max_length=100)] = None,
) -> list[StagePerformanceRow]:
    start, end = _resolve_window(days)
    return await svc.build_stage_performance(
        period_start=start, period_end=end, market=market, strategy=strategy
    )


@api_router.get("/decisions", response_model=RetrospectiveDecisionsResponse)
async def list_decisions(
    user: Annotated[User, Depends(get_current_user)],
    svc: Annotated[ResearchRetrospectiveService, Depends(_service)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Annotated[Literal["KR", "US", "CRYPTO"] | None, Query()] = None,
    strategy: Annotated[str | None, Query(max_length=100)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> RetrospectiveDecisionsResponse:
    start, end = _resolve_window(days)
    return await svc.list_decisions(
        period_start=start,
        period_end=end,
        market=market,
        strategy=strategy,
        limit=limit,
    )


router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
