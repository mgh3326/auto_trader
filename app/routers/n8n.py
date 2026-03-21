from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.timezone import now_kst
from app.schemas.n8n import (
    N8nBtcContext,
    N8nCryptoScanParams,
    N8nCryptoScanResponse,
    N8nCryptoScanSummary,
    N8nDailyBriefResponse,
    N8nFilledOrdersResponse,
    N8nKrMorningReportResponse,
    N8nMarketContextResponse,
    N8nMarketContextSummary,
    N8nMarketOverview,
    N8nPendingOrdersResponse,
    N8nPendingOrderSummary,
    N8nPendingResolveRequest,
    N8nPendingResolveResponse,
    N8nPendingReviewResponse,
    N8nPendingSnapshotsRequest,
    N8nPendingSnapshotsResponse,
    N8nTradeReviewListResponse,
    N8nTradeReviewsRequest,
    N8nTradeReviewsResponse,
    N8nTradeReviewStats,
    N8nTradeReviewStatsResponse,
)
from app.services.n8n_crypto_scan_service import fetch_crypto_scan
from app.services.n8n_daily_brief_service import fetch_daily_brief
from app.services.n8n_filled_orders_service import fetch_filled_orders
from app.services.n8n_formatting import fmt_date_with_weekday
from app.services.n8n_kr_morning_report_service import fetch_kr_morning_report
from app.services.n8n_market_context_service import fetch_market_context
from app.services.n8n_pending_orders_service import fetch_pending_orders
from app.services.n8n_pending_review_service import fetch_pending_review
from app.services.n8n_pending_snapshot_service import (
    resolve_pending_snapshots,
    save_pending_snapshots,
)
from app.services.n8n_trade_review_service import (
    get_trade_review_stats,
    get_trade_reviews,
    save_trade_reviews,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/n8n", tags=["n8n"])


@router.get("/pending-orders", response_model=N8nPendingOrdersResponse)
async def get_pending_orders(
    market: Literal["crypto", "kr", "us", "all"] = Query(
        "all", description="Market filter"
    ),
    min_amount: float = Query(0, ge=0, description="Minimum KRW amount filter"),
    include_current_price: bool = Query(
        True, description="Fetch current prices and compute gap percentage"
    ),
    side: Literal["buy", "sell"] | None = Query(None, description="Order side filter"),
    include_indicators: bool = Query(
        True, description="Include technical indicators per order"
    ),
) -> N8nPendingOrdersResponse | JSONResponse:
    as_of_dt = now_kst().replace(microsecond=0)
    as_of = as_of_dt.isoformat()

    try:
        result = await fetch_pending_orders(
            market=market,
            min_amount=min_amount,
            include_current_price=include_current_price,
            side=side,
            as_of=as_of_dt,
            include_indicators=include_indicators,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build n8n pending orders response")
        payload = N8nPendingOrdersResponse(
            success=False,
            as_of=as_of,
            market=market,
            orders=[],
            summary=N8nPendingOrderSummary(
                total=0,
                buy_count=0,
                sell_count=0,
                total_buy_krw=0.0,
                total_sell_krw=0.0,
                total_buy_fmt=None,
                total_sell_fmt=None,
                title=None,
            ),
            errors=[{"market": market, "error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nPendingOrdersResponse(
        success=bool(result.get("success", True)),
        as_of=as_of,
        market=market,
        orders=result["orders"],
        summary=N8nPendingOrderSummary(**result["summary"]),
        errors=result["errors"],
    )


@router.get("/market-context", response_model=N8nMarketContextResponse)
async def get_market_context(
    market: Literal["crypto", "kr", "us", "all"] = Query(
        "crypto", description="Market filter (crypto only for now)"
    ),
    symbols: str | None = Query(
        None,
        description="Comma-separated symbols (e.g. 'BTC,ETH,SOL'). If null, uses pending+holdings",
    ),
    include_fear_greed: bool = Query(True, description="Include Fear & Greed Index"),
    include_economic_calendar: bool = Query(
        True, description="Include today's economic events"
    ),
) -> N8nMarketContextResponse | JSONResponse:
    """
    Get comprehensive market context with technical indicators.

    Provides RSI, ADX, Stochastic RSI, trend analysis, and market sentiment
    for specified symbols. Also includes Fear & Greed Index and economic calendar.
    """
    as_of_dt = now_kst().replace(microsecond=0)
    as_of = as_of_dt.isoformat()

    symbol_list: list[str] | None = None
    if symbols:
        symbol_list = [s.strip().upper() for s in symbols.split(",") if s.strip()]

    try:
        result = await fetch_market_context(
            market=market,
            symbols=symbol_list,
            include_fear_greed=include_fear_greed,
            include_economic_calendar=include_economic_calendar,
            as_of=as_of_dt,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build n8n market context response")
        payload = N8nMarketContextResponse(
            success=False,
            as_of=as_of,
            market=market,
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            symbols=[],
            summary=N8nMarketContextSummary(
                total_symbols=0,
                bullish_count=0,
                bearish_count=0,
                neutral_count=0,
                avg_rsi=None,
                market_sentiment="neutral",
            ),
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nMarketContextResponse(
        success=True,
        as_of=as_of,
        market=market,
        market_overview=result["market_overview"],
        symbols=result["symbols"],
        summary=result["summary"],
        errors=result["errors"],
    )


@router.get("/daily-brief", response_model=N8nDailyBriefResponse)
async def get_daily_brief(
    markets: str = Query(
        "crypto,kr,us",
        description="Comma-separated market list: crypto,kr,us",
    ),
    min_amount: float = Query(
        50_000, ge=0, description="Minimum order amount filter in KRW"
    ),
) -> N8nDailyBriefResponse | JSONResponse:
    """
    Get unified daily trading brief.

    Combines pending orders, market context, portfolio summary, and yesterday's fills
    into a single response with pre-formatted brief text for Discord delivery.
    """
    as_of_dt = now_kst().replace(microsecond=0)

    market_list = [m.strip().lower() for m in markets.split(",") if m.strip()]
    valid_markets = [m for m in market_list if m in ("crypto", "kr", "us")]
    if not valid_markets:
        valid_markets = ["crypto", "kr", "us"]

    try:
        result = await fetch_daily_brief(
            markets=valid_markets,
            min_amount=min_amount,
            as_of=as_of_dt,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build daily brief")
        from app.schemas.n8n import (
            N8nDailyBriefPendingOrders,
            N8nDailyBriefPortfolio,
            N8nMarketOverview,
            N8nYesterdayFills,
        )

        payload = N8nDailyBriefResponse(
            success=False,
            as_of=as_of_dt.isoformat(),
            date_fmt=as_of_dt.strftime("%m/%d"),
            market_overview=N8nMarketOverview(
                fear_greed=None,
                btc_dominance=None,
                total_market_cap_change_24h=None,
                economic_events_today=[],
            ),
            pending_orders=N8nDailyBriefPendingOrders(),
            portfolio_summary=N8nDailyBriefPortfolio(),
            yesterday_fills=N8nYesterdayFills(),
            brief_text="",
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nDailyBriefResponse(**result)


@router.get("/filled-orders", response_model=N8nFilledOrdersResponse)
async def get_filled_orders(
    days: int = Query(1, ge=1, le=90, description="Lookback period in days"),
    markets: str = Query("crypto,kr,us", description="Comma-separated markets"),
    min_amount: float = Query(0, ge=0, description="Minimum filled amount"),
    include_indicators: bool = Query(
        False, description="Include technical indicators per order"
    ),
) -> N8nFilledOrdersResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    try:
        result = await fetch_filled_orders(
            days=days, markets=markets, min_amount=min_amount,
            include_indicators=include_indicators,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to fetch filled orders")
        payload = N8nFilledOrdersResponse(
            success=False,
            as_of=as_of,
            total_count=0,
            orders=[],
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nFilledOrdersResponse(
        success=True,
        as_of=as_of,
        total_count=len(result["orders"]),
        orders=result["orders"],
        errors=result["errors"],
    )


@router.post("/trade-reviews", response_model=N8nTradeReviewsResponse)
async def post_trade_reviews(
    body: N8nTradeReviewsRequest,
    db: AsyncSession = Depends(get_db),
) -> N8nTradeReviewsResponse | JSONResponse:
    try:
        result = await save_trade_reviews(db, [r.model_dump() for r in body.reviews])
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save trade reviews")
        return JSONResponse(
            status_code=500,
            content=N8nTradeReviewsResponse(
                success=False, saved_count=0, errors=[{"error": str(exc)}]
            ).model_dump(),
        )

    return N8nTradeReviewsResponse(
        success=True,
        saved_count=result["saved_count"],
        skipped_count=result["skipped_count"],
        errors=result["errors"],
    )


@router.get("/trade-reviews", response_model=N8nTradeReviewListResponse)
async def get_trade_reviews_endpoint(
    period: str = Query("7d", description="Duration format: 7d, 30d, 90d"),
    market: str | None = Query(None, description="Filter by market: crypto, kr, us"),
    symbol: str | None = Query(None, description="Filter by symbol (e.g. BTC, 005930)"),
    limit: int = Query(100, ge=1, le=500, description="Maximum results to return"),
    db: AsyncSession = Depends(get_db),
) -> N8nTradeReviewListResponse | JSONResponse:
    try:
        result = await get_trade_reviews(
            db, period=period, market=market, symbol=symbol, limit=limit
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get trade reviews")
        return JSONResponse(
            status_code=500,
            content=N8nTradeReviewListResponse(
                success=False,
                period="error",
                total_count=0,
                reviews=[],
                errors=[{"error": str(exc)}],
            ).model_dump(),
        )

    return N8nTradeReviewListResponse(
        success=True,
        period=result["period"],
        total_count=result["total_count"],
        reviews=result["reviews"],
        errors=result["errors"],
    )


@router.get("/trade-reviews/stats", response_model=N8nTradeReviewStatsResponse)
async def get_trade_review_stats_endpoint(
    period: str = Query("week", description="week, month, quarter"),
    market: str | None = Query(None, description="Filter by market"),
    db: AsyncSession = Depends(get_db),
) -> N8nTradeReviewStatsResponse | JSONResponse:
    try:
        stats = await get_trade_review_stats(db, period=period, market=market)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to get trade review stats")
        return JSONResponse(
            status_code=500,
            content=N8nTradeReviewStatsResponse(
                success=False,
                stats=N8nTradeReviewStats(period="error"),
                errors=[{"error": str(exc)}],
            ).model_dump(),
        )

    return N8nTradeReviewStatsResponse(
        success=True,
        stats=N8nTradeReviewStats(**stats),
        errors=[],
    )


@router.get("/pending-review", response_model=N8nPendingReviewResponse)
async def get_pending_review_endpoint(
    market: str = Query("all", description="Market filter"),
    min_amount: float = Query(0, ge=0, description="Minimum KRW amount"),
) -> N8nPendingReviewResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    try:
        result = await fetch_pending_review(market=market, min_amount=min_amount)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to fetch pending review")
        payload = N8nPendingReviewResponse(
            success=False,
            as_of=as_of,
            total_count=0,
            orders=[],
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nPendingReviewResponse(
        success=True,
        as_of=as_of,
        total_count=len(result["orders"]),
        orders=result["orders"],
        errors=result["errors"],
    )


@router.post("/pending-snapshots", response_model=N8nPendingSnapshotsResponse)
async def post_pending_snapshots(
    body: N8nPendingSnapshotsRequest,
    db: AsyncSession = Depends(get_db),
) -> N8nPendingSnapshotsResponse | JSONResponse:
    try:
        result = await save_pending_snapshots(
            db, [s.model_dump() for s in body.snapshots]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to save pending snapshots")
        return JSONResponse(
            status_code=500,
            content=N8nPendingSnapshotsResponse(
                success=False, saved_count=0, errors=[{"error": str(exc)}]
            ).model_dump(),
        )

    return N8nPendingSnapshotsResponse(
        success=True,
        saved_count=result["saved_count"],
        errors=result["errors"],
    )


@router.patch("/pending-snapshots/resolve", response_model=N8nPendingResolveResponse)
async def patch_pending_resolve(
    body: N8nPendingResolveRequest,
    db: AsyncSession = Depends(get_db),
) -> N8nPendingResolveResponse | JSONResponse:
    try:
        result = await resolve_pending_snapshots(
            db, [r.model_dump() for r in body.resolutions]
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to resolve pending snapshots")
        return JSONResponse(
            status_code=500,
            content=N8nPendingResolveResponse(
                success=False, resolved_count=0, errors=[{"error": str(exc)}]
            ).model_dump(),
        )

    return N8nPendingResolveResponse(
        success=True,
        resolved_count=result["resolved_count"],
        not_found_count=result["not_found_count"],
        errors=result["errors"],
    )


@router.get("/crypto-scan", response_model=N8nCryptoScanResponse)
async def get_crypto_scan(
    top_n: int = Query(30, ge=1, le=100, description="Top N by 24h trade amount"),
    include_holdings: bool = Query(
        True, description="Include holding coins outside top N"
    ),
    include_crash: bool = Query(True, description="Include crash detection data"),
    include_sma_cross: bool = Query(True, description="Include SMA20 cross detection"),
    include_fear_greed: bool = Query(True, description="Include Fear & Greed Index"),
    ohlcv_days: int = Query(50, ge=20, le=200, description="OHLCV lookback days"),
) -> N8nCryptoScanResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    scan_params = N8nCryptoScanParams(
        top_n=top_n,
        include_holdings=include_holdings,
        include_crash=include_crash,
        include_sma_cross=include_sma_cross,
        include_fear_greed=include_fear_greed,
        ohlcv_days=ohlcv_days,
    )

    try:
        result = await fetch_crypto_scan(
            top_n=top_n,
            include_holdings=include_holdings,
            include_crash=include_crash,
            include_sma_cross=include_sma_cross,
            include_fear_greed=include_fear_greed,
            ohlcv_days=ohlcv_days,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to build n8n crypto scan response")
        payload = N8nCryptoScanResponse(
            success=False,
            as_of=as_of,
            scan_params=scan_params,
            btc_context=N8nBtcContext(),
            fear_greed=None,
            coins=[],
            summary=N8nCryptoScanSummary(
                total_scanned=0,
                top_n_count=0,
                holdings_added=0,
            ),
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nCryptoScanResponse(
        success=result.get("success", True),
        as_of=as_of,
        scan_params=scan_params,
        btc_context=N8nBtcContext(**(result.get("btc_context") or {})),
        fear_greed=result.get("fear_greed"),
        coins=result.get("coins", []),
        summary=N8nCryptoScanSummary(**(result.get("summary", {}))),
        errors=result.get("errors", []),
    )


@router.get("/kr-morning-report", response_model=N8nKrMorningReportResponse)
async def get_kr_morning_report(
    include_screen: bool = Query(True),
    screen_strategy: str | None = Query(
        None,
        description="Optional screener strategy override. Null defaults to oversold-style RSI scan.",
    ),
    include_pending: bool = Query(True),
    top_n: int = Query(20, ge=1, le=50),
) -> N8nKrMorningReportResponse | JSONResponse:
    as_of_dt = now_kst().replace(microsecond=0)
    try:
        result = await fetch_kr_morning_report(
            include_screen=include_screen,
            screen_strategy=screen_strategy,
            include_pending=include_pending,
            top_n=top_n,
            as_of=as_of_dt,
        )
    except Exception as exc:
        logger.exception("Failed to build KR morning report")
        payload = N8nKrMorningReportResponse(
            success=False,
            as_of=as_of_dt.isoformat(),
            date_fmt=fmt_date_with_weekday(as_of_dt),
            brief_text="",
            errors=[{"error": str(exc)}],
        )
        return JSONResponse(status_code=500, content=payload.model_dump())

    return N8nKrMorningReportResponse(**result)
