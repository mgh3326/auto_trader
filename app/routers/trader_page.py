"""Read-only operator API for the /trader page (task 889, stage 1).

Every route is GET-only and sits behind the same operator session auth as
/invest (``get_authenticated_user`` + ``AuthMiddleware``). This module must
never import order, approval, proposal, or watch-mutation services — pinned by
tests/test_trader_page_safety.py.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.trader_page import (
    TraderFillsResponse,
    TraderOpenOrdersResponse,
    TraderWatchesResponse,
)
from app.services.trader_page.service import TraderPageService

router = APIRouter(prefix="/trading/api/trader", tags=["trader-page"])


def get_trader_page_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TraderPageService:
    return TraderPageService(db)


@router.get("/open-orders")
async def list_open_orders(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[TraderPageService, Depends(get_trader_page_service)],
    refresh: Annotated[bool, Query()] = False,
) -> TraderOpenOrdersResponse:
    return await service.open_orders(refresh=refresh)


@router.get("/fills/today")
async def list_fills_today(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[TraderPageService, Depends(get_trader_page_service)],
) -> TraderFillsResponse:
    return await service.fills_today()


@router.get("/watches")
async def list_watches(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[TraderPageService, Depends(get_trader_page_service)],
) -> TraderWatchesResponse:
    return await service.active_watches()
