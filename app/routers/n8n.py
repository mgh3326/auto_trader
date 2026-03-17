from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.timezone import now_kst
from app.schemas.n8n import (
    N8nFilledOrdersResponse,
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
    N8nTradeReviewsRequest,
    N8nTradeReviewsResponse,
    N8nTradeReviewStats,
    N8nTradeReviewStatsResponse,
)
from app.services.n8n_filled_orders_service import fetch_filled_orders
from app.services.n8n_market_context_service import fetch_market_context
from app.services.n8n_pending_orders_service import fetch_pending_orders
from app.services.n8n_pending_review_service import fetch_pending_review
from app.services.n8n_pending_snapshot_service import (
    resolve_pending_snapshots,
    save_pending_snapshots,
)
from app.services.n8n_trade_review_service import (
    get_trade_review_stats,
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
    attention_only: bool = Query(
        False, description="Return only orders that need attention"
    ),
    near_fill_pct: float = Query(
        2.0, ge=0.1, le=50.0, description="Near fill threshold percentage"
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
            attention_only=attention_only,
            near_fill_pct=near_fill_pct,
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
                near_fill_count=0,
                needs_attention_count=0,
                attention_orders_only=[],
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


@router.get("/filled-orders", response_model=N8nFilledOrdersResponse)
async def get_filled_orders(
    days: int = Query(1, ge=1, le=90, description="Lookback period in days"),
    markets: str = Query("crypto,kr,us", description="Comma-separated markets"),
    min_amount: float = Query(0, ge=0, description="Minimum filled amount"),
) -> N8nFilledOrdersResponse | JSONResponse:
    as_of = now_kst().replace(microsecond=0).isoformat()
    try:
        result = await fetch_filled_orders(
            days=days, markets=markets, min_amount=min_amount
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
