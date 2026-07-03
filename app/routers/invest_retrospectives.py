"""FastAPI router for /invest retrospectives read surface (ROB-662)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.symbol import to_db_symbol
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_retrospectives import (
    NextActionRow,
    NextActionsResponse,
    RetrospectiveRow,
    RetrospectivesResponse,
)
from app.schemas.trade_retrospective import (
    VALID_ROOT_CAUSE_CLASSES,
    VALID_TRIGGER_TYPES,
)
from app.services.trade_journal import trade_retrospective_service as retro_svc

router = APIRouter(
    prefix="/trading/api/invest/retrospectives",
    tags=["invest-retrospectives"],
)

Market = Literal["all", "kr", "us", "crypto"]


def _normalize_symbol(symbol: str | None, market: Market) -> str | None:
    if not symbol:
        return None
    sym = symbol.strip()
    return to_db_symbol(sym) if market == "us" else sym.upper()


@router.get("")
async def list_retrospectives(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[Market, Query()] = "all",
    trigger_type: Annotated[str | None, Query()] = None,
    root_cause_class: Annotated[str | None, Query()] = None,
    symbol: Annotated[str | None, Query()] = None,
    days: Annotated[int | None, Query(ge=1)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RetrospectivesResponse:
    if trigger_type is not None and trigger_type not in VALID_TRIGGER_TYPES:
        raise HTTPException(status_code=422, detail=f"invalid trigger_type: {trigger_type}")
    if root_cause_class is not None and root_cause_class not in VALID_ROOT_CAUSE_CLASSES:
        raise HTTPException(
            status_code=422, detail=f"invalid root_cause_class: {root_cause_class}"
        )
    db_symbol = _normalize_symbol(symbol, market)
    result = await retro_svc.get_retrospectives(
        db,
        market=None if market == "all" else market,
        trigger_type=trigger_type,
        root_cause_class=root_cause_class,
        symbol=db_symbol,
        days=days,
        limit=limit,
        offset=offset,
    )
    summary = result["summary"]
    return RetrospectivesResponse(
        market=market,
        trigger_type=trigger_type,
        root_cause_class=root_cause_class,
        symbol=db_symbol,
        count=summary["count"],
        total=summary["total"],
        items=[RetrospectiveRow(**e) for e in result["entries"]],
        as_of=datetime.now(UTC),
    )


@router.get("/next-actions")
async def list_open_next_actions(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    market: Annotated[Market, Query()] = "all",
    symbol: Annotated[str | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
) -> NextActionsResponse:
    statuses = (
        frozenset(s.strip() for s in status.split(",") if s.strip())
        if status
        else None
    )
    db_symbol = _normalize_symbol(symbol, market)
    result = await retro_svc.get_open_next_actions(
        db,
        market=None if market == "all" else market,
        symbol=db_symbol,
        statuses=statuses,
    )
    return NextActionsResponse(
        market=market,
        symbol=db_symbol,
        count=result["count"],
        scan_limit=result["scan_limit"],
        items=[NextActionRow(**i) for i in result["items"]],
    )