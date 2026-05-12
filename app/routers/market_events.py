"""Read-only market events router (ROB-128).

GET only. No mutation. Auth required (matches existing trading/api pattern).
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.calendar_freshness import CoverageMatrixResponse
from app.schemas.market_events import (
    MarketEventsDayResponse,
    MarketEventsRangeResponse,
)
from app.schemas.market_events_calendar import DiscoverCalendarResponse
from app.schemas.market_events_freshness import MarketEventsFreshnessResponse
from app.services.market_events.discover_calendar import DiscoverCalendarService
from app.services.market_events.freshness_service import (
    DEFAULT_STALE_THRESHOLD_HOURS,
    MarketEventsFreshnessService,
)
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
    "/api/market-events/freshness",
    response_model=MarketEventsFreshnessResponse,
)
async def get_market_events_freshness(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[
        date | None, Query(description="ISO start date, inclusive")
    ] = None,
    to_date: Annotated[
        date | None, Query(description="ISO end date, inclusive")
    ] = None,
    stale_threshold_hours: float = DEFAULT_STALE_THRESHOLD_HOURS,
) -> MarketEventsFreshnessResponse:
    """Return read-only market-events ingestion freshness diagnostics."""
    if (from_date is None) ^ (to_date is None):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="from_date and to_date must both be provided or both omitted",
        )
    today = date.today()
    window_from = from_date or (today - timedelta(days=7))
    window_to = to_date or (today + timedelta(days=7))
    svc = MarketEventsFreshnessService(db)
    try:
        return await svc.compute(
            window_from=window_from,
            window_to=window_to,
            stale_threshold_hours=stale_threshold_hours,
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


@router.get(
    "/api/market-events/coverage",
    response_model=CoverageMatrixResponse,
)
async def get_market_events_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    from_date: Annotated[date, Query(description="ISO start date, inclusive")],
    to_date: Annotated[date, Query(description="ISO end date, inclusive")],
) -> CoverageMatrixResponse:
    svc = MarketEventsFreshnessService(db)
    try:
        return await svc.get_coverage_matrix(from_date, to_date)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
