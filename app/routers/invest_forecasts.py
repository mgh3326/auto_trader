"""FastAPI router for /invest forecast calibration read surface (ROB-663).

Read-only exposure of ``review.trade_forecasts`` (ROB-650): calibration cohorts
(Brier / hit-rate / calibration-gap), the scoring-due open queue, and recent
scored history. Scoring (``forecast_resolve``) stays MCP-only — the web reads,
never writes; no broker/order/watch mutation is reachable from here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_forecasts import (
    VALID_GROUP_BY,
    VALID_INSTRUMENT_TYPES,
    CalibrationGroupRow,
    CalibrationResponse,
    ForecastListResponse,
    ForecastRow,
)
from app.services.trade_journal import forecast_service as fc_svc

router = APIRouter(
    prefix="/trading/api/invest/forecasts",
    tags=["invest-forecasts"],
)


def _validate_instrument_type(instrument_type: str | None) -> None:
    if instrument_type is not None and instrument_type not in VALID_INSTRUMENT_TYPES:
        raise HTTPException(
            status_code=422, detail=f"invalid instrument_type: {instrument_type}"
        )


@router.get("/calibration")
async def get_forecast_calibration(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    group_by: Annotated[str, Query()] = "created_by",
    created_by: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    instrument_type: Annotated[str | None, Query()] = None,
    days: Annotated[int | None, Query(ge=1)] = None,
) -> CalibrationResponse:
    if group_by not in VALID_GROUP_BY:
        raise HTTPException(status_code=422, detail=f"invalid group_by: {group_by}")
    _validate_instrument_type(instrument_type)
    result = await fc_svc.build_forecast_calibration_aggregate(
        db,
        group_by=group_by,
        created_by=created_by,
        symbol=symbol,
        instrument_type=instrument_type,
        days=days,
    )
    groups = result["groups"]
    return CalibrationResponse(
        group_by=result["group_by"],
        created_by=created_by,
        symbol=symbol,
        instrument_type=instrument_type,
        days=days,
        count=len(groups),
        groups=[CalibrationGroupRow(**g) for g in groups],
        as_of=datetime.now(UTC),
    )


@router.get("/open")
async def list_open_forecasts(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    symbol: Annotated[str | None, Query()] = None,
    created_by: Annotated[str | None, Query()] = None,
    instrument_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ForecastListResponse:
    _validate_instrument_type(instrument_type)
    result = await fc_svc.list_open_forecasts(
        db,
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
        limit=limit,
    )
    return ForecastListResponse(
        kind="open",
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
        count=result["summary"]["count"],
        items=[ForecastRow(**e) for e in result["entries"]],
        as_of=datetime.now(UTC),
    )


@router.get("/closed")
async def list_closed_forecasts(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    symbol: Annotated[str | None, Query()] = None,
    created_by: Annotated[str | None, Query()] = None,
    instrument_type: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ForecastListResponse:
    _validate_instrument_type(instrument_type)
    result = await fc_svc.list_closed_forecasts(
        db,
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
        limit=limit,
    )
    return ForecastListResponse(
        kind="closed",
        symbol=symbol,
        created_by=created_by,
        instrument_type=instrument_type,
        count=result["summary"]["count"],
        items=[ForecastRow(**e) for e in result["entries"]],
        as_of=datetime.now(UTC),
    )
