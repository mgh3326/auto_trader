"""Read-only market events router (ROB-128).

GET only. No mutation. Auth required (matches existing trading/api pattern).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.market_events import (
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
)
from app.schemas.market_events_calendar import DiscoverCalendarResponse
from app.services.market_events.discover_calendar import DiscoverCalendarService
from app.services.market_events.query_service import MarketEventsQueryService
from app.services.market_events.user_context import get_user_event_context

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


@router.get(
    "/api/market-events/discover-calendar",
    response_model=DiscoverCalendarResponse,
)
async def get_discover_calendar(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[date, Query(description="ISO start date, inclusive")],
    to_date: Annotated[date, Query(description="ISO end date, inclusive")],
    today: Annotated[
        date | None, Query(description="ISO today; default = server clock")
    ] = None,
    tab: Annotated[
        Literal["all", "economic", "earnings"],
        Query(description="UI tab filter"),
    ] = "all",
) -> DiscoverCalendarResponse:
    target_today = today or date.today()
    query_service = MarketEventsQueryService(db)
    ctx = await get_user_event_context(db, user_id=current_user.id)
    svc = DiscoverCalendarService(query_service=query_service)
    try:
        return await svc.build(
            from_date=from_date,
            to_date=to_date,
            today=target_today,
            ctx=ctx,
            tab=tab,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
