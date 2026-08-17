"""Read-only market-calendar router backed by the Toss calendar (ROB-1280).

GET only. No mutation. Delegates entirely to the existing
``app.services.brokers.toss.market_calendar`` service — no new Toss token,
credential, or client surface is introduced here.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal

from fastapi import APIRouter, Query

from app.core.config import settings
from app.schemas.market_calendar import (
    MarketCalendarResponse,
    MarketCalendarSessionWindow,
    MarketCalendarSessionWindows,
)
from app.services.brokers.toss.market_calendar import (
    TossKrMarketDay,
    TossUsMarketDay,
    get_toss_market_calendar,
)

router = APIRouter(prefix="/trading", tags=["market-calendar"])

Market = Literal["kr", "us"]

_KST = dt.timezone(dt.timedelta(hours=9))


def _session_window(
    window: object,
) -> MarketCalendarSessionWindow | None:
    if window is None:
        return None
    return MarketCalendarSessionWindow(start=window.start, end=window.end)


def _is_open_and_windows(
    day: TossKrMarketDay | TossUsMarketDay,
) -> tuple[bool, MarketCalendarSessionWindows]:
    if isinstance(day, TossKrMarketDay):
        windows = MarketCalendarSessionWindows(
            pre_market=_session_window(day.pre_market),
            regular_market=_session_window(day.regular_market),
            after_market=_session_window(day.after_market),
        )
        is_open = day.regular_market is not None
        return is_open, windows

    windows = MarketCalendarSessionWindows(
        day_market=_session_window(day.day_market),
        pre_market=_session_window(day.pre_market),
        regular_market=_session_window(day.regular_market),
        after_market=_session_window(day.after_market),
    )
    is_open = day.regular_market is not None
    return is_open, windows


@router.get(
    "/api/market-calendar/{market}/today",
    response_model=MarketCalendarResponse,
)
async def get_market_calendar_today(
    market: Market,
    date: Annotated[
        dt.date | None,
        Query(description="ISO date; default = today (KST)"),
    ] = None,
) -> MarketCalendarResponse:
    query_date = date or dt.datetime.now(_KST).date()
    as_of = dt.datetime.now(dt.UTC)

    if not settings.toss_api_enabled:
        return MarketCalendarResponse(
            date=query_date,
            market=market,
            is_open=None,
            source="toss_calendar",
            unavailable_reason="toss_api_disabled",
            as_of=as_of,
        )

    calendar = await get_toss_market_calendar(market, query_date)
    if calendar is None:
        return MarketCalendarResponse(
            date=query_date,
            market=market,
            is_open=None,
            source="toss_calendar",
            unavailable_reason="toss_calendar_unavailable",
            as_of=as_of,
        )

    day = calendar.day_for(query_date)
    if day is None:
        return MarketCalendarResponse(
            date=query_date,
            market=market,
            is_open=None,
            source="toss_calendar",
            unavailable_reason="date_out_of_calendar_window",
            as_of=as_of,
        )

    is_open, windows = _is_open_and_windows(day)
    return MarketCalendarResponse(
        date=query_date,
        market=market,
        is_open=is_open,
        session_windows=windows,
        source="toss_calendar",
        as_of=as_of,
    )
