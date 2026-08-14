"""Authenticated B0/B1 loss-cut evidence and web confirmation endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.role_hierarchy import has_min_role
from app.core.config import settings
from app.core.db import get_db
from app.models.trading import User, UserRole
from app.routers.dependencies import get_authenticated_user
from app.routers.invest_api import get_invest_home_service
from app.schemas.loss_cut_approval import (
    LossCutBeginRequest,
    LossCutBeginResponse,
    LossCutConfirmRequest,
    LossCutConfirmResponse,
    LossCutEvidenceResponse,
)
from app.services.invest_home_service import InvestHomeService
from app.services.order_proposals.errors import (
    OrderProposalError,
    OrderProposalNotFound,
)
from app.services.order_proposals.loss_cut_approval import (
    LossCutApprovalRejected,
    LossCutApprovalService,
)

router = APIRouter(prefix="/invest/api", tags=["invest-loss-cut-approvals"])


async def require_loss_cut_operator(
    user: Annotated[User, Depends(get_authenticated_user)],
) -> User:
    """JSON role gate: raise 403 instead of returning an HTML response."""
    if not has_min_role(user.role, UserRole.trader):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Trader role required",
        )
    return user


def get_loss_cut_approval_service(
    db: Annotated[AsyncSession, Depends(get_db)],
    home_service: Annotated[InvestHomeService, Depends(get_invest_home_service)],
) -> LossCutApprovalService:
    return LossCutApprovalService(db, home_service=home_service)


def _require_evidence_enabled() -> None:
    if not settings.INVEST_LOSS_CUT_EVIDENCE_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _require_approval_enabled() -> None:
    if not settings.INVEST_LOSS_CUT_APPROVAL_ENABLED:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


@router.get(
    "/loss-cut-evidence/{symbol}",
    response_model=LossCutEvidenceResponse,
    dependencies=[Depends(_require_evidence_enabled)],
)
async def get_loss_cut_evidence(
    symbol: str,
    response: Response,
    user: Annotated[User, Depends(require_loss_cut_operator)],
    service: Annotated[LossCutApprovalService, Depends(get_loss_cut_approval_service)],
) -> LossCutEvidenceResponse:
    _no_store(response)
    return await service.get_symbol_evidence(symbol=symbol, user_id=user.id)


@router.get(
    "/loss-cut-approvals/{proposal_id}",
    response_model=LossCutEvidenceResponse,
    dependencies=[Depends(_require_approval_enabled)],
)
async def get_loss_cut_approval(
    proposal_id: uuid.UUID,
    response: Response,
    _user: Annotated[User, Depends(require_loss_cut_operator)],
    service: Annotated[LossCutApprovalService, Depends(get_loss_cut_approval_service)],
) -> LossCutEvidenceResponse:
    _no_store(response)
    try:
        return await service.get_proposal_evidence(proposal_id=proposal_id)
    except OrderProposalNotFound as exc:
        raise HTTPException(status_code=404, detail="proposal_not_found") from exc
    except OrderProposalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/loss-cut-approvals/{proposal_id}/begin",
    response_model=LossCutBeginResponse,
    dependencies=[Depends(_require_approval_enabled)],
)
async def begin_loss_cut_approval(
    proposal_id: uuid.UUID,
    _body: LossCutBeginRequest,
    response: Response,
    user: Annotated[User, Depends(require_loss_cut_operator)],
    service: Annotated[LossCutApprovalService, Depends(get_loss_cut_approval_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LossCutBeginResponse:
    _no_store(response)
    try:
        result = await service.begin(
            proposal_id=proposal_id,
            actor_user_id=user.id,
            actor_role=user.role,
        )
        await db.commit()
        return result
    except LossCutApprovalRejected as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except OrderProposalNotFound as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="proposal_not_found") from exc
    except OrderProposalError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        await db.rollback()
        raise


@router.post(
    "/loss-cut-approvals/{proposal_id}/confirm",
    response_model=LossCutConfirmResponse,
    dependencies=[Depends(_require_approval_enabled)],
)
async def confirm_loss_cut_approval(
    proposal_id: uuid.UUID,
    body: LossCutConfirmRequest,
    response: Response,
    user: Annotated[User, Depends(require_loss_cut_operator)],
    service: Annotated[LossCutApprovalService, Depends(get_loss_cut_approval_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> LossCutConfirmResponse:
    _no_store(response)
    try:
        result = await service.confirm(
            proposal_id=proposal_id,
            ceremony_id=body.ceremony_id,
            actor_user_id=user.id,
            actor_role=user.role,
        )
        await db.commit()
        return result
    except LossCutApprovalRejected as exc:
        await db.commit()
        raise HTTPException(status_code=409, detail=exc.code) from exc
    except OrderProposalNotFound as exc:
        await db.rollback()
        raise HTTPException(status_code=404, detail="proposal_not_found") from exc
    except OrderProposalError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        await db.rollback()
        raise


__all__ = [
    "get_loss_cut_approval_service",
    "require_loss_cut_operator",
    "router",
]
