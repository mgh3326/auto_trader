from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.core.timezone import now_kst
from app.schemas.n8n import (
    N8nDailyBriefResponse,
    N8nMarketContextResponse,
    N8nMarketContextSummary,
    N8nMarketOverview,
    N8nPendingOrdersResponse,
    N8nPendingOrderSummary,
)
from app.services.n8n_daily_brief_service import fetch_daily_brief
from app.services.n8n_market_context_service import fetch_market_context
from app.services.n8n_pending_orders_service import fetch_pending_orders

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
