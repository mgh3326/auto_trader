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
from app.schemas.invest_coverage import CoverageMarket, InvestCoverageResponse
from app.schemas.invest_feed_news import FeedNewsResponse, FeedTab, FeedTopic
from app.schemas.invest_feed_research import (
    FeedResearchFilters,
    FeedResearchResponse,
    FeedResearchTab,
)
from app.schemas.invest_fx_dashboard import FxDashboardResponse
from app.schemas.invest_home import InvestHomeResponse
from app.schemas.invest_market_dashboard import MarketDashboardResponse
from app.schemas.invest_momentum_events import (
    MomentumCoverageResponse,
    MomentumEventItem,
    MomentumEventsResponse,
    ThemeEventItem,
    ThemeEventsResponse,
)
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
from app.schemas.investor_flow import InvestorFlowResponse
from app.services.invest_coverage_service import build_invest_coverage
from app.services.invest_home_service import InvestHomeService
from app.services.invest_momentum_events.coverage_service import build_momentum_coverage
from app.services.invest_momentum_events.repository import (
    InvestMomentumEventSnapshotsRepository,
)
from app.services.invest_screener_snapshots.coverage_service import build_coverage
from app.services.invest_view_model.account_panel_service import build_account_panel
from app.services.invest_view_model.calendar_service import build_calendar
from app.services.invest_view_model.feed_news_service import build_feed_news
from app.services.invest_view_model.feed_research_service import build_feed_research
from app.services.invest_view_model.fx_dashboard_service import build_fx_dashboard
from app.services.invest_view_model.investor_flow_service import (
    build_investor_flow_cards,
)
from app.services.invest_view_model.market_dashboard_service import (
    build_market_dashboard,
)
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


@router.get("/market")
async def get_market_dashboard(
    user: Annotated[Any, Depends(get_authenticated_user)],
) -> MarketDashboardResponse:
    """Read-only Naver-style market/index dashboard source (ROB-198)."""
    _ = user
    return await build_market_dashboard()


@router.get("/market/fx/dashboard")
async def get_fx_dashboard(
    user: Annotated[Any, Depends(get_authenticated_user)],
) -> FxDashboardResponse:
    """Read-only FX·macro dashboard contract fixture (ROB-216)."""
    _ = user
    return await build_fx_dashboard()


@router.get("/coverage")
async def get_invest_coverage(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: CoverageMarket = Query("kr"),
    symbols: str = Query(
        "", description="Optional comma-separated symbols for per-symbol coverage"
    ),
    as_of: Annotated[date | None, Query(alias="asOf")] = None,
) -> InvestCoverageResponse:
    """Read-only Toss-parity data coverage dashboard source (ROB-192)."""
    _ = user
    symbol_list = [part.strip() for part in symbols.split(",") if part.strip()]
    try:
        return await build_invest_coverage(
            db,
            market=market,
            symbols=symbol_list,
            as_of=as_of,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/account-panel")
async def get_account_panel(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> AccountPanelResponse:
    return await build_account_panel(user_id=user.id, db=db, home_service=service)


@router.get("/investor-flow")
async def get_investor_flow(
    user: Annotated[Any, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    symbols: str = Query("", description="Comma-separated KR symbols"),
    market: Literal["kr"] = Query("kr"),
    as_of: Annotated[date | None, Query(alias="asOf")] = None,
    max_stale_days: Annotated[int, Query(alias="maxStaleDays", ge=0, le=30)] = 1,
) -> InvestorFlowResponse:
    _ = user
    symbol_list = [part.strip() for part in symbols.split(",") if part.strip()]
    try:
        return await build_investor_flow_cards(
            db=db,
            symbols=symbol_list,
            market=market,
            as_of=as_of,
            max_stale_days=max_stale_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    topic: FeedTopic | None = Query(None),
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
        topic=topic,
    )


@router.get("/feed/research")
async def get_feed_research(
    user: Annotated[Any, Depends(get_authenticated_user)],
    service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
    tab: FeedResearchTab = Query("top"),
    limit: int = Query(30, ge=1, le=100),
    cursor: str | None = Query(None),
    source: str | None = Query(None),
    symbol: str | None = Query(None),
    analyst: str | None = Query(None),
    category: str | None = Query(None),
    query: str | None = Query(None),
    from_date: date | None = Query(None, alias="fromDate"),
    to_date: date | None = Query(None, alias="toDate"),
) -> FeedResearchResponse:
    if from_date and to_date and from_date > to_date:
        raise HTTPException(status_code=400, detail="fromDate must be <= toDate")
    home = await service.get_home(user_id=user.id)
    resolver = await build_relation_resolver(
        db, user_id=user.id, held_pairs=_held_pairs_from_home(home)
    )
    try:
        return await build_feed_research(
            db=db,
            resolver=resolver,
            tab=tab,
            limit=limit,
            cursor_str=cursor,
            filters=FeedResearchFilters(
                source=source,
                symbol=symbol,
                analyst=analyst,
                category=category,
                query=query,
                from_date=from_date,
                to_date=to_date,
            ),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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


@router.get("/momentum/events", response_model=MomentumEventsResponse)
async def get_momentum_events(
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Literal["kr", "us", "crypto"] = Query("kr"),
    snapshot_date: Annotated[date | None, Query(alias="date")] = None,
    surface: str | None = Query(None),
    order_type: Annotated[str | None, Query(alias="orderType")] = None,
    trade_type: Annotated[str | None, Query(alias="tradeType")] = None,
    limit: int = Query(50, ge=1, le=100),
) -> MomentumEventsResponse:
    """Read-only persisted Naver momentum snapshots; never fetches Naver on request."""
    if market != "kr":
        return MomentumEventsResponse(
            market=market,
            data_state="unsupported",
            empty_reason="naver_stock_supports_kr_only",
            items=[],
        )
    rows = await InvestMomentumEventSnapshotsRepository(db).list_momentum_events(
        trading_date=snapshot_date,
        surface=surface,
        order_type=order_type,
        trade_type=trade_type,
        limit=limit,
    )
    return MomentumEventsResponse(
        market="kr",
        data_state="fresh" if rows else "missing",
        empty_reason=None if rows else "no_naver_momentum_snapshots",
        items=[MomentumEventItem.model_validate(row) for row in rows],
    )


@router.get("/momentum/themes", response_model=ThemeEventsResponse)
async def get_momentum_themes(
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Literal["kr", "us", "crypto"] = Query("kr"),
    snapshot_date: Annotated[date | None, Query(alias="date")] = None,
    event_kind: Annotated[
        Literal["theme", "upjong"] | None, Query(alias="eventKind")
    ] = None,
    sort_type: Annotated[str | None, Query(alias="sortType")] = None,
    limit: int = Query(50, ge=1, le=100),
) -> ThemeEventsResponse:
    """Read-only persisted Naver theme/upjong snapshots; never fetches Naver on request."""
    if market != "kr":
        return ThemeEventsResponse(
            market=market,
            data_state="unsupported",
            empty_reason="naver_stock_supports_kr_only",
            items=[],
        )
    rows = await InvestMomentumEventSnapshotsRepository(db).list_theme_events(
        trading_date=snapshot_date,
        event_kind=event_kind,
        sort_type=sort_type,
        limit=limit,
    )
    return ThemeEventsResponse(
        market="kr",
        data_state="fresh" if rows else "missing",
        empty_reason=None if rows else "no_naver_theme_snapshots",
        items=[ThemeEventItem.model_validate(row) for row in rows],
    )


@router.get("/momentum/coverage", response_model=MomentumCoverageResponse)
async def momentum_coverage(
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Literal["kr", "us", "crypto"] = Query("kr"),
    as_of: Annotated[date | None, Query(alias="asOf")] = None,
) -> MomentumCoverageResponse:
    """Read-only coverage summary for Naver momentum/theme snapshots (ROB-222)."""
    report = await build_momentum_coverage(db, market=market, as_of=as_of)
    return MomentumCoverageResponse(
        market=report.market,
        as_of=report.asOf,
        momentum_events=report.momentumEvents,
        theme_events=report.themeEvents,
        last_momentum_snapshot_at=report.lastMomentumSnapshotAt,
        last_theme_snapshot_at=report.lastThemeSnapshotAt,
        data_state=report.dataState,
        empty_reason=report.emptyReason,
    )
