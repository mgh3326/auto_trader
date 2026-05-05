"""ROB-116 — Portfolio action board router (read-only)."""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.portfolio_actions import PortfolioActionsResponse
from app.services.portfolio_action_service import PortfolioActionService

api_router = APIRouter(prefix="/api/portfolio-actions", tags=["portfolio-actions"])
router = APIRouter()


def get_portfolio_action_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PortfolioActionService:
    return PortfolioActionService(db)


@api_router.get("", response_model=PortfolioActionsResponse)
async def get_portfolio_actions(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[PortfolioActionService, Depends(get_portfolio_action_service)],
    market: Annotated[
        Literal["KR", "US", "CRYPTO"] | None,
        Query(description="Optional market filter"),
    ] = None,
) -> PortfolioActionsResponse:
    return await service.build_action_board(
        user_id=current_user.id, market_filter=market
    )


router.include_router(api_router)
router.include_router(api_router, prefix="/trading")
