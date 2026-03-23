from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse

from app.core.templates import templates
from app.services.n8n_pending_orders_service import fetch_pending_orders

router = APIRouter(tags=["Pending Orders"])


@router.get("/pending", response_class=HTMLResponse)
async def pending_orders_dashboard(request: Request):
    """Render the pending orders dashboard HTML page."""
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        "pending_orders_dashboard.html",
        {
            "request": request,
            "user": user,
        },
    )


@router.get("/api/pending/orders")
async def api_pending_orders(
    market: Literal["all", "crypto", "kr", "us"] = Query("all"),
    side: Literal["buy", "sell"] | None = Query(None),
    min_amount: float = Query(0.0),
):
    """Return pending orders as JSON for the dashboard."""
    return await fetch_pending_orders(
        market=market,
        side=side,
        min_amount=min_amount,
        include_current_price=True,
        include_indicators=True,
    )
