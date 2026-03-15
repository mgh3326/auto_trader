from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from app.core.timezone import now_kst
from app.schemas.n8n import N8nPendingOrdersResponse, N8nPendingOrderSummary
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
