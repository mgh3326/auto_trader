"""FastAPI router for watch alerts in the /invest panel (ROB-591)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_watches import WatchesResponse
from app.services.invest_view_model.watch_panel_service import WatchPanelService

router = APIRouter(
    prefix="/trading/api/invest/watches",
    tags=["invest-watches"],
)

Market = Literal["all", "kr", "us", "crypto"]
Status = Literal["all", "active", "triggered", "expired", "canceled"]


def get_watch_panel_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> WatchPanelService:
    return WatchPanelService(db=db)


@router.get("")
async def list_watches(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[WatchPanelService, Depends(get_watch_panel_service)],
    market: Annotated[Market, Query()] = "all",
    status: Annotated[Status, Query()] = "all",
) -> WatchesResponse:
    return await service.list_watches(market=market, status=status)
