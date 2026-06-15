"""Read-only /invest execution-fill endpoints (ROB-211)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.execution_ledger import (
    ExecutionLedgerFreshnessReport,
    ExecutionLedgerListResponse,
    Side,
)
from app.services.execution_ledger.query_service import ExecutionLedgerQueryService

router = APIRouter(prefix="/trading/api/invest/fills", tags=["invest-fills"])
Market = Literal["kr", "us", "crypto"]


@router.get("/recent")
async def recent_fills(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    market: Market | None = None,
    side: Side | None = None,
) -> ExecutionLedgerListResponse:
    return await ExecutionLedgerQueryService(db).list_recent(
        limit=limit,
        market=market,
        side=side,
    )


@router.get("/by-symbol/{symbol}")
async def fills_by_symbol(
    symbol: str,
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
) -> ExecutionLedgerListResponse:
    return await ExecutionLedgerQueryService(db).list_by_symbol(
        symbol=symbol.strip().upper(), days=days
    )


@router.get("/sell-history")
async def sell_history(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    market: Market | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> ExecutionLedgerListResponse:
    return await ExecutionLedgerQueryService(db).list_sell_history(
        days=days, market=market, limit=limit
    )


@router.get("/freshness")
async def fills_freshness(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExecutionLedgerFreshnessReport:
    return await ExecutionLedgerQueryService(db).freshness()
