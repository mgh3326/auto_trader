"""Read-only /invest execution-fill endpoints (ROB-211)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.review import KISLiveOrderLedger, LiveOrderLedger, TossLiveOrderLedger
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.execution_ledger import (
    ExecutionLedgerFreshnessReport,
    ExecutionLedgerListResponse,
    Side,
)
from app.schemas.investment_reports import LinkedOrderView
from app.services.execution_ledger.query_service import ExecutionLedgerQueryService
from app.services.investment_reports.linked_orders import (
    project_kis_live_order,
    project_live_order,
    project_toss_live_order,
)

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


@router.get("/order-detail")
async def order_detail(
    _user: Annotated[User, Depends(get_authenticated_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    broker: Annotated[str, Query(min_length=1)],
    ledger_id: Annotated[int, Query(ge=1)],
) -> LinkedOrderView:
    """Single order-ledger row for the standalone order detail view (§57차 item ②).

    Read-only: reuses the ROB-554 projection helpers (``linked_orders.py``) so
    this view can never drift from the stock-detail order-ledger card. The
    three live ledgers (LiveOrderLedger for US/crypto, KISLiveOrderLedger for
    KR, TossLiveOrderLedger for Toss KR/US) each have an independent id
    sequence, so ``broker`` disambiguates which table ``ledger_id`` refers to
    (mirrors ``linkedOrderKey`` on the frontend, which keys on broker+market+id
    for the same reason).
    """
    broker_key = broker.strip().lower()
    if broker_key == "kis":
        row = await db.get(KISLiveOrderLedger, ledger_id)
        view = project_kis_live_order(row) if row is not None else None
    elif broker_key == "toss":
        row = await db.get(TossLiveOrderLedger, ledger_id)
        view = project_toss_live_order(row) if row is not None else None
    else:
        row = await db.get(LiveOrderLedger, ledger_id)
        view = project_live_order(row) if row is not None else None

    if view is None:
        raise HTTPException(status_code=404, detail="order_not_found")
    return view
