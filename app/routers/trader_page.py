"""Read-only operator API for the /trader page (tasks 889 and 890).

Every route is GET-only and sits behind the same operator session auth as
/invest (``get_authenticated_user`` + ``AuthMiddleware``). This module must
never import order, approval, proposal, or watch-mutation services — pinned by
tests/test_trader_page_safety.py.

Task 890 PR A adds the approval inbox reads. The inbox buttons do not post
here: they call the existing /invest web approval endpoints
(``app/routers/invest_loss_cut_approvals.py``), so no approval route exists in
this module.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.role_hierarchy import has_min_role
from app.core.db import get_db
from app.core.timezone import now_kst
from app.models.trading import User, UserRole
from app.routers.dependencies import get_authenticated_user
from app.schemas.trader_approvals import (
    TraderApprovalDetailResponse,
    TraderApprovalInboxResponse,
)
from app.schemas.trader_page import (
    TraderFillsResponse,
    TraderOpenOrdersResponse,
    TraderWatchesResponse,
)
from app.services.trader_page.approval_inbox import TraderApprovalInboxService
from app.services.trader_page.service import TraderPageService

router = APIRouter(prefix="/trading/api/trader", tags=["trader-page"])


def get_trader_page_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TraderPageService:
    return TraderPageService(db)


@router.get("/open-orders")
async def list_open_orders(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[TraderPageService, Depends(get_trader_page_service)],
    refresh: Annotated[bool, Query()] = False,
) -> TraderOpenOrdersResponse:
    return await service.open_orders(refresh=refresh)


@router.get("/fills/today")
async def list_fills_today(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[TraderPageService, Depends(get_trader_page_service)],
) -> TraderFillsResponse:
    return await service.fills_today()


@router.get("/watches")
async def list_watches(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[TraderPageService, Depends(get_trader_page_service)],
) -> TraderWatchesResponse:
    return await service.active_watches()


async def require_trader_operator(
    user: Annotated[User, Depends(get_authenticated_user)],
) -> User:
    """Same role gate and rejection as the /invest approval hub.

    Mirrors ``require_loss_cut_operator`` in
    ``app/routers/invest_loss_cut_approvals.py`` (403 ``Trader role
    required``). Not imported from there: that module imports the approval
    execution core, which this read-only module must never load.
    """
    if not has_min_role(user.role, UserRole.trader):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Trader role required",
        )
    return user


def get_trader_approval_inbox_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TraderApprovalInboxService:
    return TraderApprovalInboxService(db, now=now_kst())


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get("/approvals")
async def list_approval_inbox(
    response: Response,
    _user: Annotated[User, Depends(require_trader_operator)],
    service: Annotated[
        TraderApprovalInboxService, Depends(get_trader_approval_inbox_service)
    ],
) -> TraderApprovalInboxResponse:
    _no_store(response)
    return await service.list_inbox()


@router.get("/approvals/{proposal_id}")
async def get_approval_inbox_item(
    proposal_id: uuid.UUID,
    response: Response,
    _user: Annotated[User, Depends(require_trader_operator)],
    service: Annotated[
        TraderApprovalInboxService, Depends(get_trader_approval_inbox_service)
    ],
) -> TraderApprovalDetailResponse:
    _no_store(response)
    detail = await service.get_item(proposal_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="proposal_not_found")
    return detail
