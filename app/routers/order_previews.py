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

logger = logging.getLogger(__name__)

api_router = APIRouter(prefix="/trading/api/order-previews", tags=["order-previews"])
router = APIRouter()


def get_order_preview_session_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OrderPreviewSessionService:
    from app.services.order_preview_dry_run import OrderIntentDryRunRunner

    return OrderPreviewSessionService(db=db, dry_run=OrderIntentDryRunRunner())


def get_broker_submit_callable():
    """Return the broker submit callable for this route.

    ROB-118 only persists preview/approval records. The default production path is
    deliberately disabled so an approval token alone cannot reach live broker
    submission. Tests, paper-only experiments, or a later explicitly scoped PR may
    override this dependency with a mock/paper submitter.
    """

    return None


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
        return await service.get(user_id=current_user.id, preview_uuid=preview_uuid)
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
    if broker_submit is None:
        raise HTTPException(
            status_code=409,
            detail="submit blocked: broker submission disabled for ROB-118 preview MVP",
        )

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
