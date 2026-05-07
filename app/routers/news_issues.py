# app/routers/news_issues.py
"""Market issue clustering router (ROB-130).

Read-only. No order/watch/intent/broker imports allowed.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.news_issues import MarketIssuesResponse
from app.services.news_issue_clustering_service import build_market_issues

router = APIRouter(prefix="/trading", tags=["news-issues"])


@router.get("/api/news-issues", response_model=MarketIssuesResponse)
async def get_news_issues(
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market: Literal["all", "kr", "us", "crypto"] = Query("all"),
    window_hours: int = Query(24, ge=1, le=168),
    limit: int = Query(20, ge=1, le=100),
) -> MarketIssuesResponse:
    return await build_market_issues(
        market=market, window_hours=window_hours, limit=limit
    )
