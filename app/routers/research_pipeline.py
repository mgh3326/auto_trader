"""Research pipeline API router."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.research_pipeline import (
    ResearchSessionCreateRequest,
    ResearchSessionCreateResponse,
)
from app.services.research_pipeline_service import ResearchPipelineService

api_router = APIRouter(prefix="/api/research-pipeline", tags=["research-pipeline"])
router = APIRouter()


def check_pipeline_enabled():
    if not settings.RESEARCH_PIPELINE_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="research_pipeline_disabled",
        )


@api_router.post(
    "/sessions",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(check_pipeline_enabled)],
)
async def create_session(
    payload: ResearchSessionCreateRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> ResearchSessionCreateResponse:
    service = ResearchPipelineService(db)
    return await service.create_session_and_dispatch(
        symbol=payload.symbol,
        name=payload.name,
        instrument_type=payload.instrument_type,
        research_run_id=payload.research_run_id,
        user_id=current_user.id,
    )


@api_router.get("/sessions", dependencies=[Depends(check_pipeline_enabled)])
async def list_sessions(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    limit: int = 20,
) -> list[dict[str, Any]]:
    service = ResearchPipelineService(db)
    return await service.list_recent_sessions(limit=limit)


@api_router.get(
    "/sessions/{session_id}", dependencies=[Depends(check_pipeline_enabled)]
)
async def get_session(
    session_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    include: str | None = None,
) -> dict[str, Any]:
    service = ResearchPipelineService(db)
    if include == "full":
        full = await service.get_session_full(session_id)
        if not full:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="session_not_found",
            )
        return full

    session = await service.get_session(session_id)
    if not session:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="session_not_found",
        )
    return session


@api_router.get(
    "/sessions/{session_id}/stages", dependencies=[Depends(check_pipeline_enabled)]
)
async def get_session_stages(
    session_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> list[dict[str, Any]]:
    service = ResearchPipelineService(db)
    return await service.get_latest_stages(session_id)


@api_router.get(
    "/sessions/{session_id}/summary", dependencies=[Depends(check_pipeline_enabled)]
)
async def get_session_summary(
    session_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> dict[str, Any]:
    service = ResearchPipelineService(db)
    summary = await service.get_latest_summary(session_id)
    if not summary:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="summary_not_found",
        )
    return summary


@api_router.get(
    "/symbols/{symbol}/timeline",
    dependencies=[Depends(check_pipeline_enabled)],
)
async def get_symbol_timeline(
    symbol: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    days: int = 30,
) -> dict[str, Any]:
    if days < 1 or days > 365:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="days_out_of_range",
        )
    service = ResearchPipelineService(db)
    return await service.get_symbol_timeline(symbol, days=days)


# Keep the legacy `/api/research-pipeline` surface from ROB-112 and expose the
# Trading Decision Workspace alias used by the React app's shared API client
# (`/trading/api/...`).
router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
