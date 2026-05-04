"""Preopen dashboard API router (ROB-39).

Read-only. No order/watch/intent/broker imports allowed in this file.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.preopen import PreopenLatestResponse
from app.services import preopen_dashboard_service

router = APIRouter(prefix="/trading", tags=["preopen-dashboard"])


@router.get("/api/preopen/latest", response_model=PreopenLatestResponse)
async def get_latest_preopen(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market_scope: Literal["kr", "us"] = "kr",
    stage: Literal["preopen", "us_open"] | None = None,
) -> PreopenLatestResponse:
    return await preopen_dashboard_service.get_latest_preopen_dashboard(
        db,
        user_id=current_user.id,
        market_scope=market_scope,
        stage=stage,
    )
