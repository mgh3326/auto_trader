"""Read-only market events router (ROB-128).

GET only. No mutation. Auth required (matches existing trading/api pattern).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.market_events import (
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
)
from app.services.market_events.query_service import MarketEventsQueryService

router = APIRouter(prefix="/trading", tags=["market-events"])


@router.get(
    "/api/market-events/today",
    response_model=MarketEventsDayResponse,
)
async def get_today_market_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    on_date: Annotated[
        date | None, Query(description="ISO date; default = today")
    ] = None,
    category: str | None = None,
    market: str | None = None,
    source: str | None = None,
) -> MarketEventsDayResponse:
    target = on_date or date.today()
    svc = MarketEventsQueryService(db)
    try:
        return await svc.list_for_date(
            target, category=category, market=market, source=source
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.get(
    "/api/market-events/range",
    response_model=MarketEventsRangeResponse,
)
async def get_market_events_range(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[date, Query(description="ISO start date, inclusive")],
    to_date: Annotated[date, Query(description="ISO end date, inclusive")],
    category: str | None = None,
    market: str | None = None,
    source: str | None = None,
) -> MarketEventsRangeResponse:
    svc = MarketEventsQueryService(db)
    try:
        return await svc.list_for_range(
            from_date, to_date, category=category, market=market, source=source
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
