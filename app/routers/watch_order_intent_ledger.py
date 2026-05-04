"""Read-only watch order intent ledger router (ROB-103).

GET endpoints only. No POST/PATCH/DELETE. No broker mutation.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.review import WatchOrderIntentLedger
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user

router = APIRouter(prefix="/trading", tags=["watch-order-intent-ledger"])


def serialize_ledger_row(row: WatchOrderIntentLedger) -> dict:
    return {
        "id": row.id,
        "correlation_id": row.correlation_id,
        "idempotency_key": row.idempotency_key,
        "market": row.market,
        "target_kind": row.target_kind,
        "symbol": row.symbol,
        "condition_type": row.condition_type,
        "threshold": float(row.threshold) if row.threshold is not None else None,
        "threshold_key": row.threshold_key,
        "action": row.action,
        "side": row.side,
        "account_mode": row.account_mode,
        "execution_source": row.execution_source,
        "lifecycle_state": row.lifecycle_state,
        "quantity": float(row.quantity) if row.quantity is not None else None,
        "limit_price": float(row.limit_price) if row.limit_price is not None else None,
        "notional": float(row.notional) if row.notional is not None else None,
        "currency": row.currency,
        "notional_krw_input": (
            float(row.notional_krw_input)
            if row.notional_krw_input is not None
            else None
        ),
        "max_notional_krw": (
            float(row.max_notional_krw) if row.max_notional_krw is not None else None
        ),
        "notional_krw_evaluated": (
            float(row.notional_krw_evaluated)
            if row.notional_krw_evaluated is not None
            else None
        ),
        "fx_usd_krw_used": (
            float(row.fx_usd_krw_used) if row.fx_usd_krw_used is not None else None
        ),
        "approval_required": row.approval_required,
        "execution_allowed": row.execution_allowed,
        "blocking_reasons": row.blocking_reasons,
        "blocked_by": row.blocked_by,
        "detail": row.detail,
        "preview_line": row.preview_line,
        "triggered_value": (
            float(row.triggered_value) if row.triggered_value is not None else None
        ),
        "kst_date": row.kst_date,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/api/watch/order-intent/ledger/recent")
async def list_recent(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
    market: str | None = None,
    lifecycle_state: str | None = None,
    kst_date: str | None = None,
    limit: int = 20,
) -> dict:
    capped = max(1, min(limit, 100))
    stmt = select(WatchOrderIntentLedger).order_by(
        WatchOrderIntentLedger.created_at.desc()
    )
    if market is not None:
        stmt = stmt.where(WatchOrderIntentLedger.market == market.strip().lower())
    if lifecycle_state is not None:
        stmt = stmt.where(
            WatchOrderIntentLedger.lifecycle_state == lifecycle_state.strip().lower()
        )
    if kst_date is not None:
        stmt = stmt.where(WatchOrderIntentLedger.kst_date == kst_date.strip())
    stmt = stmt.limit(capped)
    rows = (await db.execute(stmt)).scalars().all()
    return {"count": len(rows), "items": [serialize_ledger_row(r) for r in rows]}


@router.get("/api/watch/order-intent/ledger/{correlation_id}")
async def get_by_correlation(
    correlation_id: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[User, Depends(get_authenticated_user)],
) -> dict:
    row = (
        await db.execute(
            select(WatchOrderIntentLedger).where(
                WatchOrderIntentLedger.correlation_id == correlation_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not_found")
    return serialize_ledger_row(row)
