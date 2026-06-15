"""Read-only /invest current open-order endpoint (ROB-572)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.open_orders import OpenOrdersResponse
from app.services.current_orders_service import CurrentOrdersService

router = APIRouter(
    prefix="/trading/api/invest/open-orders",
    tags=["invest-open-orders"],
)

Market = Literal["all", "kr", "us", "crypto"]


def get_current_orders_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> CurrentOrdersService:
    return CurrentOrdersService(db=db)


@router.get("")
async def list_open_orders(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[CurrentOrdersService, Depends(get_current_orders_service)],
    market: Annotated[Market, Query()] = "all",
) -> OpenOrdersResponse:
    return await service.list_open_orders(market=market)
