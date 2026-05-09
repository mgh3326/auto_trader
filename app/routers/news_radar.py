# app/routers/news_radar.py
"""Market Risk News Radar router (ROB-109).

Read-only. No order/watch/intent/broker imports allowed in this file.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.routers.pagination import PaginationParams, pagination_params
from app.schemas.news_radar import (
    NewsRadarMarket,
    NewsRadarResponse,
    NewsRadarRiskCategory,
)
from app.services.news_radar_service import build_news_radar

router = APIRouter(prefix="/trading", tags=["news-radar"])

_pagination = pagination_params(default_limit=50, max_limit=200)


@router.get("/api/news-radar", response_model=NewsRadarResponse)
async def get_news_radar(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market: NewsRadarMarket = Query("all"),
    hours: int = Query(24, ge=1, le=168),
    q: str | None = Query(None, max_length=200),
    risk_category: NewsRadarRiskCategory | None = Query(None),
    include_excluded: bool = Query(True),
    p: PaginationParams = Depends(_pagination),
) -> NewsRadarResponse:
    return await build_news_radar(
        market=market,
        hours=hours,
        q=q,
        risk_category=risk_category,
        include_excluded=include_excluded,
        limit=p.limit,
    )
