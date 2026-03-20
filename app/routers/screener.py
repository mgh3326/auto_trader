from __future__ import annotations

from typing import Any, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.analysis.models import PriceAnalysis
from app.core.templates import templates
from app.routers.openclaw_callback import _require_openclaw_callback_token
from app.services.screener_service import ScreenerService

router = APIRouter(tags=["Screener"])

_screener_service_singleton: ScreenerService | None = None


def get_screener_service() -> ScreenerService:
    global _screener_service_singleton
    if _screener_service_singleton is None:
        _screener_service_singleton = ScreenerService()
    return _screener_service_singleton


class ScreenerFilterRequest(BaseModel):
    market: Literal["kr", "us", "crypto"] = "kr"
    asset_type: Literal["stock", "etf", "etn"] | None = None
    category: str | None = None
    sector: str | None = None
    strategy: str | None = None
    sort_by: str | None = None
    sort_order: Literal["asc", "desc"] = "desc"
    min_market_cap: float | None = None
    max_per: float | None = None
    max_pbr: float | None = None
    min_dividend_yield: float | None = None
    min_dividend: float | None = None
    min_analyst_buy: float | None = None
    max_rsi: float | None = None
    min_volume: float | None = None
    limit: int = Field(default=50, ge=1, le=100)


class ScreenerReportRequest(BaseModel):
    market: Literal["kr", "us", "crypto"]
    symbol: str = Field(min_length=1)
    name: str | None = None


class ScreenerCallbackRequest(BaseModel):
    request_id: str
    symbol: str
    name: str
    instrument_type: str
    decision: Literal["buy", "hold", "sell"]
    confidence: int = Field(ge=0, le=100)
    reasons: list[str] | None = None
    price_analysis: PriceAnalysis
    detailed_text: str | None = None


class ScreenerOrderRequest(BaseModel):
    market: Literal["kr", "us", "crypto"]
    symbol: str
    side: Literal["buy", "sell"]
    order_type: Literal["limit", "market"] = "limit"
    quantity: float | None = None
    price: float | None = None
    amount: float | None = None
    confirm: bool = False
    reason: str = ""


@router.get("/screener", response_class=HTMLResponse)
async def screener_dashboard(request: Request):
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        "screener_dashboard.html",
        {
            "request": request,
            "user": user,
            "poll_interval_seconds": 3,
            "poll_timeout_seconds": 120,
            "queued_poll_interval_seconds": 7,
            "running_poll_interval_seconds": 3,
            "poll_backoff_max_seconds": 20,
        },
    )


@router.get("/screener/report/{job_id}", response_class=HTMLResponse)
async def screener_report_page(request: Request, job_id: str):
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        "screener_report_detail.html",
        {
            "request": request,
            "user": user,
            "poll_interval_seconds": 3,
            "poll_timeout_seconds": 120,
            "queued_poll_interval_seconds": 7,
            "running_poll_interval_seconds": 3,
            "poll_backoff_max_seconds": 20,
            "initial_job_id": job_id,
        },
    )


@router.get("/api/screener/list")
async def screener_list(
    market: Literal["kr", "us", "crypto"] = "kr",
    asset_type: Literal["stock", "etf", "etn"] | None = None,
    category: str | None = None,
    sector: str | None = None,
    strategy: str | None = None,
    sort_by: str | None = None,
    sort_order: Literal["asc", "desc"] = "desc",
    min_market_cap: float | None = Query(default=None),
    max_per: float | None = Query(default=None),
    max_pbr: float | None = Query(default=None),
    min_dividend_yield: float | None = Query(default=None),
    min_dividend: float | None = Query(default=None),
    min_analyst_buy: float | None = Query(default=None),
    max_rsi: float | None = Query(default=None),
    min_volume: float | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    service: ScreenerService = Depends(get_screener_service),
):
    try:
        request_kwargs: dict[str, object | None] = {
            "market": market,
            "asset_type": asset_type,
            "category": category,
            "strategy": strategy,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "min_market_cap": min_market_cap,
            "max_per": max_per,
            "max_pbr": max_pbr,
            "max_rsi": max_rsi,
            "min_volume": min_volume,
            "limit": limit,
        }
        if min_dividend_yield is not None or min_dividend is None:
            request_kwargs["min_dividend_yield"] = min_dividend_yield
        if sector is not None:
            request_kwargs["sector"] = sector
        if min_dividend is not None:
            request_kwargs["min_dividend"] = min_dividend
        if min_analyst_buy is not None:
            request_kwargs["min_analyst_buy"] = min_analyst_buy
        return await service.list_screening(**cast(Any, request_kwargs))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/api/screener/refresh")
async def screener_refresh(
    payload: ScreenerFilterRequest,
    service: ScreenerService = Depends(get_screener_service),
):
    try:
        request_kwargs = payload.model_dump()
        if (
            request_kwargs.get("min_dividend") is not None
            and request_kwargs.get("min_dividend_yield") is None
        ):
            request_kwargs.pop("min_dividend_yield", None)
        for key in ("sector", "min_dividend", "min_analyst_buy"):
            if request_kwargs.get(key) is None:
                request_kwargs.pop(key, None)
        return await service.refresh_screening(**cast(Any, request_kwargs))
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc


@router.post("/api/screener/report")
async def screener_request_report(
    payload: ScreenerReportRequest,
    service: ScreenerService = Depends(get_screener_service),
):
    return await service.request_report(
        market=payload.market,
        symbol=payload.symbol,
        name=payload.name,
    )


@router.get("/api/screener/report/{job_id}")
async def screener_report_status(
    job_id: str,
    service: ScreenerService = Depends(get_screener_service),
):
    return await service.get_report_status(job_id)


@router.post("/api/screener/callback")
async def screener_callback(
    payload: ScreenerCallbackRequest,
    _: None = Depends(_require_openclaw_callback_token),
    service: ScreenerService = Depends(get_screener_service),
):
    return await service.process_callback(payload.model_dump(exclude_none=True))


@router.post("/api/screener/order")
async def screener_order(
    payload: ScreenerOrderRequest,
    service: ScreenerService = Depends(get_screener_service),
):
    return await service.place_order(
        market=payload.market,
        symbol=payload.symbol,
        side=payload.side,
        order_type=payload.order_type,
        quantity=payload.quantity,
        price=payload.price,
        amount=payload.amount,
        confirm=payload.confirm,
        reason=payload.reason,
    )
