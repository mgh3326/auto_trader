"""ROB-123 — read-only `/invest/api`.

이 라우터는 `InvestHomeService` 만 의존하고 broker / KIS / Upbit 클라이언트를 직접
import 하지 않는다. order / watch / scheduler / mutation 경로 import 금지.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
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
from app.schemas.invest_screener import (
    ScreenerPresetsResponse,
    ScreenerResultsResponse,
)
from app.schemas.invest_signals import SignalsResponse, SignalTab
from app.schemas.invest_stock_detail import (
    StockDetailCandlesResponse,
    StockDetailOrdersResponse,
    StockDetailResponse,
)
from app.services.invest_home_service import InvestHomeService
from app.services.invest_screener_snapshots.coverage_service import build_coverage
from app.services.invest_view_model.account_panel_service import build_account_panel
from app.services.invest_view_model.calendar_service import build_calendar
from app.services.invest_view_model.feed_news_service import build_feed_news
from app.services.invest_view_model.relation_resolver import build_relation_resolver
from app.services.invest_view_model.screener_service import (
    build_screener_presets,
    build_screener_results,
)
from app.services.invest_view_model.signals_service import build_signals
from app.services.invest_view_model.stock_detail_candles_service import (
    UnsupportedPeriod,
    build_stock_detail_candles,
)
from app.services.invest_view_model.stock_detail_orders_service import (
    build_stock_detail_orders,
)
from app.services.invest_view_model.stock_detail_service import build_stock_detail
from app.services.invest_view_model.stock_detail_symbol_resolver import (
    SymbolNotFound,
    resolve_symbol,
)
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


def get_screener_service_dep():
    """Lazy DI for the existing read-only screening service.

    The import is intentionally inside the function so that importing the
    router module does not transitively load `app.services.screener_service`
    and its `app.services.kis*` chain — see tests/test_invest_api_router_safety.py.
    """
    from app.services.screener_service import ScreenerService

    return ScreenerService()


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


StockDetailMarketParam = Literal["kr", "us", "crypto"]


@router.get("/stock-detail/{market}/{symbol}")
async def get_stock_detail(
    market: StockDetailMarketParam,
    symbol: str,
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StockDetailResponse:
    try:
        return await build_stock_detail(
            user_id=user.id,
            market=market,
            symbol=symbol,
            db=db,
        )
    except SymbolNotFound as exc:
        raise HTTPException(status_code=404, detail="symbol_not_found") from exc


@router.get("/stock-detail/{market}/{symbol}/candles")
async def get_stock_detail_candles(
    market: StockDetailMarketParam,
    symbol: str,
    user: Annotated[Any, Depends(get_authenticated_user)],
    period: str = Query("1d"),
) -> StockDetailCandlesResponse:
    _ = user
    try:
        return await build_stock_detail_candles(
            market=market,
            symbol=symbol,
            period=period,
        )
    except UnsupportedPeriod as exc:
        raise HTTPException(status_code=400, detail=exc.code) from exc


@router.get("/stock-detail/{market}/{symbol}/news")
async def get_stock_detail_news(
    market: StockDetailMarketParam,
    symbol: str,
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
) -> FeedNewsResponse:
    try:
        resolved = await resolve_symbol(market, symbol, db)
    except SymbolNotFound as exc:
        raise HTTPException(status_code=404, detail="symbol_not_found") from exc
    resolver = await build_relation_resolver(db, user_id=user.id, held_pairs=[])
    return await build_feed_news(
        db=db,
        resolver=resolver,
        tab=market,
        limit=limit,
        cursor=cursor,
        include_quotes=False,
        symbol_filter=(resolved.symbol_db, market),
    )


@router.get("/stock-detail/{market}/{symbol}/orders")
async def get_stock_detail_orders(
    market: StockDetailMarketParam,
    symbol: str,
    user: Annotated[Any, Depends(get_authenticated_user)],
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    days: int = Query(90, ge=1, le=365),
) -> StockDetailOrdersResponse:
    _ = user
    return await build_stock_detail_orders(
        market=market,
        symbol=symbol,
        days=days,
        limit=limit,
        cursor=cursor,
    )


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
        db=db,
        resolver=resolver,
        from_date=from_date,
        to_date=to_date,
        tab=tab,
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
    include_quotes: Annotated[bool, Query(alias="includeQuotes")] = False,
) -> FeedNewsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_feed_news(
        db=db,
        resolver=resolver,
        tab=tab,
        limit=limit,
        cursor=cursor,
        include_quotes=include_quotes,
    )


@router.get("/screener/presets")
async def get_screener_presets_endpoint(
    user: Annotated[Any, Depends(get_authenticated_user)],
) -> ScreenerPresetsResponse:
    return build_screener_presets()


@router.get("/screener/results")
async def get_screener_results_endpoint(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    screening_service: Annotated[Any, Depends(get_screener_service_dep)],
    preset: str = Query(..., min_length=1),
    market: Literal["kr", "us"] = Query("kr"),
) -> ScreenerResultsResponse:
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    return await build_screener_results(
        preset_id=preset,
        screening_service=screening_service,
        resolver=resolver,
        market=market,
        session=db,
    )


@router.get("/screener/snapshots/coverage")
async def screener_snapshots_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Literal["kr", "us"] = Query("kr"),
) -> dict:
    """Read-only coverage summary for invest_screener_snapshots (ROB-170)."""
    report = await build_coverage(db, market=market)
    return {
        "market": report.market,
        "asOf": report.asOf.isoformat(),
        "totalSymbolsInUniverse": report.totalSymbolsInUniverse,
        "snapshotsCoveringToday": report.snapshotsCoveringToday,
        "snapshotsStale": report.snapshotsStale,
        "snapshotsMissing": report.snapshotsMissing,
        "lastComputedAt": report.lastComputedAt.isoformat()
        if report.lastComputedAt
        else None,
        "dataState": report.dataState,
    }
