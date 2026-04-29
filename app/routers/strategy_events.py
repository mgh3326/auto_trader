"""ROB-41 strategy events API.

Read/write metadata only. NO broker / order / watch / paper / live execution
imports. NO mutation of trading_decision_proposals or actions.
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.strategy_events import (
    StrategyEventCreateRequest,
    StrategyEventDetail,
    StrategyEventListResponse,
)
from app.services import strategy_event_service

router = APIRouter(prefix="/trading", tags=["strategy-events"])


@router.post(
    "/api/strategy-events",
    response_model=StrategyEventDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_strategy_event(
    request: StrategyEventCreateRequest,
    response: Response,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> StrategyEventDetail:
    try:
        detail = await strategy_event_service.create_strategy_event(
            db, request=request, user_id=current_user.id
        )
    except strategy_event_service.UnknownSessionUUIDError:
        raise HTTPException(status_code=404, detail="session_uuid_not_found")
    await db.commit()
    response.headers["Location"] = f"/trading/api/strategy-events/{detail.event_uuid}"
    return detail


@router.get("/api/strategy-events", response_model=StrategyEventListResponse)
async def list_strategy_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    session_uuid: UUID | None = Query(default=None),
    mine: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> StrategyEventListResponse:
    try:
        return await strategy_event_service.list_strategy_events(
            db,
            session_uuid=session_uuid,
            user_id=current_user.id if mine else None,
            limit=limit,
            offset=offset,
        )
    except strategy_event_service.UnknownSessionUUIDError:
        raise HTTPException(status_code=404, detail="session_uuid_not_found")


@router.get("/api/strategy-events/{event_uuid}", response_model=StrategyEventDetail)
async def get_strategy_event(
    event_uuid: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> StrategyEventDetail:
    detail = await strategy_event_service.get_strategy_event_by_uuid(
        db, event_uuid=event_uuid
    )
    if detail is None:
        raise HTTPException(status_code=404, detail="strategy_event_not_found")
    return detail
