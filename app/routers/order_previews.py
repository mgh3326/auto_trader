"""ROB-118 — Order preview/approval/submit router."""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.order_preview_session import (
    CreatePreviewRequest,
    PreviewSessionOut,
    SubmitPreviewRequest,
)
from app.services.order_preview_session_service import (
    OrderPreviewSessionService,
    PreviewNotApprovedError,
    PreviewSchemaMismatchError,
    PreviewSessionNotFoundError,
)
from app.services.orders.service import place_order

logger = logging.getLogger(__name__)

api_router = APIRouter(
    prefix="/trading/api/order-previews", tags=["order-previews"]
)
router = APIRouter()


def get_order_preview_session_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderPreviewSessionService:
    from app.services.order_preview_dry_run import OrderIntentDryRunRunner

    return OrderPreviewSessionService(db=db, dry_run=OrderIntentDryRunRunner())


def get_broker_submit_callable():
    """Return an async callable (leg, session) -> {"order_id": str}.

    Wraps app.services.orders.service.place_order. Tests override this.
    """

    async def _submit(*, leg, session):
        result = await place_order(
            symbol=session.symbol,
            market=session.market,
            side=session.side,
            order_type=leg.order_type,
            quantity=float(leg.quantity),
            price=float(leg.price) if leg.price is not None else None,
        )
        return {"order_id": result.order_id, "raw": result.raw}

    return _submit


@api_router.post("", response_model=PreviewSessionOut)
async def create_preview(
    payload: CreatePreviewRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
) -> PreviewSessionOut:
    return await service.create_preview(user_id=current_user.id, request=payload)


@api_router.post("/{preview_uuid}/refresh", response_model=PreviewSessionOut)
async def refresh_preview(
    preview_uuid: str,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
) -> PreviewSessionOut:
    try:
        return await service.refresh_preview(
            user_id=current_user.id, preview_uuid=preview_uuid
        )
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="preview not found")


@api_router.get("/{preview_uuid}", response_model=PreviewSessionOut)
async def get_preview(
    preview_uuid: str,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
) -> PreviewSessionOut:
    try:
        return await service.get(
            user_id=current_user.id, preview_uuid=preview_uuid
        )
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="preview not found")


@api_router.post("/{preview_uuid}/submit", response_model=PreviewSessionOut)
async def submit_preview(
    preview_uuid: str,
    payload: SubmitPreviewRequest,
    current_user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[
        OrderPreviewSessionService, Depends(get_order_preview_session_service)
    ],
    broker_submit=Depends(get_broker_submit_callable),
) -> PreviewSessionOut:
    try:
        return await service.submit_preview(
            user_id=current_user.id,
            preview_uuid=preview_uuid,
            request=payload,
            broker_submit=broker_submit,
        )
    except PreviewSessionNotFoundError:
        raise HTTPException(status_code=404, detail="preview not found")
    except PreviewNotApprovedError as exc:
        raise HTTPException(status_code=409, detail=f"submit blocked: {exc}")
    except PreviewSchemaMismatchError as exc:
        raise HTTPException(
            status_code=409, detail=f"schema mismatch (fail-closed): {exc}"
        )


router.include_router(api_router)
