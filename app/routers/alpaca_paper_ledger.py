"""Read-only Alpaca Paper order ledger router (ROB-84).

GET paths only. No POST/PATCH/DELETE. No broker mutation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.alpaca_paper_ledger import (
    AlpacaPaperOrderLedgerListResponse,
    AlpacaPaperOrderLedgerRead,
)
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService

router = APIRouter(prefix="/trading", tags=["alpaca-paper-ledger"])


@router.get(
    "/api/alpaca-paper/ledger/recent",
    response_model=AlpacaPaperOrderLedgerListResponse,
)
async def list_recent_ledger_entries(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    limit: int = 50,
    lifecycle_state: str | None = None,
) -> AlpacaPaperOrderLedgerListResponse:
    svc = AlpacaPaperLedgerService(db)
    rows = await svc.list_recent(limit=limit, lifecycle_state=lifecycle_state)
    return AlpacaPaperOrderLedgerListResponse(
        count=len(rows),
        items=[AlpacaPaperOrderLedgerRead.model_validate(r) for r in rows],
    )


@router.get(
    "/api/alpaca-paper/ledger/by-client-order-id/{client_order_id}",
    response_model=AlpacaPaperOrderLedgerRead,
)
async def get_ledger_by_client_order_id(
    client_order_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> AlpacaPaperOrderLedgerRead:
    svc = AlpacaPaperLedgerService(db)
    row = await svc.get_by_client_order_id(client_order_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ledger entry not found for client_order_id={client_order_id!r}",
        )
    return AlpacaPaperOrderLedgerRead.model_validate(row)


@router.get(
    "/api/alpaca-paper/ledger/{ledger_id}",
    response_model=AlpacaPaperOrderLedgerRead,
)
async def get_ledger_by_id(
    ledger_id: int,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> AlpacaPaperOrderLedgerRead:
    svc = AlpacaPaperLedgerService(db)
    row = await svc.get_by_id(ledger_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Ledger entry not found for id={ledger_id}",
        )
    return AlpacaPaperOrderLedgerRead.model_validate(row)
