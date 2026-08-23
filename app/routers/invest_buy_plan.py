"""Read-only /invest 매수 계획 (트리거 보드) endpoint — §144차.

GET only. The handler composes existing read models and the operator-owned
policy file; no route here reaches an order, proposal, watch, or broker
mutation path.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_buy_plan import BuyPlanResponse
from app.services.invest_view_model.buy_plan.service import BuyPlanService

router = APIRouter(
    prefix="/trading/api/invest/buy-plan",
    tags=["invest-buy-plan"],
)

Market = Literal["all", "kr", "us", "crypto"]


def get_buy_plan_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BuyPlanService:
    # Imported lazily for the same reason as invest_api.get_invest_home_service:
    # importing this router module must not drag in the KIS/Upbit reader chain.
    from app.routers.invest_api import get_invest_home_service
    from app.services.current_orders_service import CurrentOrdersService
    from app.services.invest_view_model.watch_panel_service import WatchPanelService

    return BuyPlanService(
        home_service=get_invest_home_service(db),
        watch_service=WatchPanelService(db=db),
        open_orders_service=CurrentOrdersService(db=db),
    )


@router.get("")
async def get_buy_plan(
    user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[BuyPlanService, Depends(get_buy_plan_service)],
    market: Annotated[Market, Query()] = "all",
) -> BuyPlanResponse:
    return await service.build(user_id=user.id, market=market)
