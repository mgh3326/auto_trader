"""ROB-123 — read-only `/invest/api`.

이 라우터는 `InvestHomeService` 만 의존하고 broker / KIS / Upbit 클라이언트를 직접
import 하지 않는다. order / watch / scheduler / mutation 경로 import 금지.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_account_panel import AccountPanelResponse
from app.schemas.invest_calendar import (
    CalendarResponse,
    CalendarTab,
    WeeklySummaryResponse,
)
from app.schemas.invest_feed_news import FeedNewsResponse, FeedTab
from app.schemas.invest_home import InvestHomeResponse
from app.schemas.invest_signals import SignalsResponse, SignalTab
from app.services.invest_home_service import InvestHomeService
from app.services.invest_view_model.account_panel_service import build_account_panel
from app.services.invest_view_model.calendar_service import build_calendar
from app.services.invest_view_model.feed_news_service import build_feed_news
from app.services.invest_view_model.relation_resolver import build_relation_resolver
from app.services.invest_view_model.signals_service import build_signals
from app.services.invest_view_model.weekly_summary_service import build_weekly_summary

router = APIRouter(prefix="/invest/api", tags=["invest"])


def get_invest_home_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InvestHomeService:
    from app.services.invest_home_readers import (
        KISHomeReader,
        ManualHomeReader,
        SafeKISClient,
        UpbitHomeReader,
    )
    from app.services.invest_quote_service import InvestQuoteService

    kis_client = SafeKISClient()
    quote_service = InvestQuoteService(kis_client, db)

    return InvestHomeService(
        kis_reader=KISHomeReader(db),
        upbit_reader=UpbitHomeReader(db),
        manual_reader=ManualHomeReader(db, quote_service=quote_service),
    )


@router.get("/home")
async def get_home(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
) -> InvestHomeResponse:
    return await service.get_home(user_id=user.id)


@router.get("/account-panel")
async def get_account_panel(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountPanelResponse:
    return await build_account_panel(user_id=user.id, db=db, home_service=service)


def _held_pairs_from_home(home) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for h in home.groupedHoldings:
        m = h.market.lower()
        if m in ("kr", "us", "crypto"):
            pairs.append((m, h.symbol))
    return pairs


@router.get("/signals")
async def get_signals(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tab: SignalTab = Query("mine"),
    limit: int = Query(20, ge=1, le=100),
) -> SignalsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_signals(db=db, resolver=resolver, tab=tab, limit=limit)


@router.get("/calendar")
async def get_calendar(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    from_date: date = Query(...),
    to_date: date = Query(...),
    tab: CalendarTab = Query("all"),
) -> CalendarResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_calendar(
        db=db, resolver=resolver, from_date=from_date, to_date=to_date, tab=tab,
    )


@router.get("/calendar/weekly-summary")
async def get_calendar_weekly_summary(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    week_start: date = Query(...),
) -> WeeklySummaryResponse:
    return await build_weekly_summary(db=db, week_start=week_start)


@router.get("/feed/news")
async def get_feed_news(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tab: FeedTab = Query("top"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
) -> FeedNewsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_feed_news(
        db=db, resolver=resolver, tab=tab, limit=limit, cursor=cursor
    )
