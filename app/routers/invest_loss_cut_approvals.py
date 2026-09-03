"""Authenticated B0/B1 loss-cut evidence and web confirmation endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.role_hierarchy import has_min_role
from app.core.config import settings
from app.core.db import get_db
from app.core.timezone import now_kst
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
from app.services.order_proposals.service import OrderProposalsService
from app.services.order_proposals.telegram_callback import handle_web_approval
from app.services.order_proposals.web_approvals import WebApprovalService

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


def _require_idempotency_key(value: str | None) -> str:
    """Require a bounded request key without exposing the server nonce.

    The request key gives clients a stable per-click identity.  The actual
    single-use approval nonce stays server-side and is checked by the shared
    execution core, never taken from a browser header or body.
    """
    normalized = (value or "").strip()
    if not normalized or len(normalized) > 128:
        raise HTTPException(status_code=400, detail="idempotency_key_required")
    return normalized


def _web_processing_response(result: dict) -> HTTPException | None:
    """Turn an in-flight/replayed core result into the polling contract."""
    reason = str(result.get("reason") or "")
    if reason in {
        "lease_held",
        "approval_nonce_used",
        "approval_nonce_replayed",
        "callback_nonce_already_used",
    }:
        return HTTPException(status_code=409, detail={"error": "processing"})
    return None


async def _execute_web_approval(
    *, proposal_id: uuid.UUID, action: str, user: User, idempotency_key: str
) -> dict:
    """Invoke the shared execution core as a web principal.

    ``idempotency_key`` is deliberately admitted here but not forwarded as an
    approval nonce; substituting browser input for the server nonce would
    weaken the published-binding gate.
    """
    del idempotency_key
    result = await handle_web_approval(
        proposal_id,
        action=action,
        actor_subject=f"user:{user.id}",
        now=now_kst(),
    )
    processing = _web_processing_response(result)
    if processing is not None:
        raise processing
    return result


@router.get("/approvals")
async def list_web_approvals(
    response: Response,
    _user: Annotated[User, Depends(require_loss_cut_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    """Return card previews with one joined DB query and no broker reads."""
    _no_store(response)
    cards = await WebApprovalService(
        OrderProposalsService(db), now=now_kst()
    ).list_cards()
    return {"items": cards}


@router.get("/approvals/{proposal_id}")
async def get_web_approval(
    proposal_id: uuid.UUID,
    response: Response,
    _user: Annotated[User, Depends(require_loss_cut_operator)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> dict:
    _no_store(response)
    try:
        return await WebApprovalService(
            OrderProposalsService(db), now=now_kst()
        ).get_card(proposal_id)
    except OrderProposalNotFound as exc:
        raise HTTPException(status_code=404, detail="proposal_not_found") from exc


@router.post("/approvals/{proposal_id}/approve")
async def approve_web_approval(
    proposal_id: uuid.UUID,
    response: Response,
    user: Annotated[User, Depends(require_loss_cut_operator)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    _no_store(response)
    return await _execute_web_approval(
        proposal_id=proposal_id,
        action="approve",
        user=user,
        idempotency_key=_require_idempotency_key(idempotency_key),
    )


@router.post("/approvals/{proposal_id}/deny")
async def deny_web_approval(
    proposal_id: uuid.UUID,
    response: Response,
    user: Annotated[User, Depends(require_loss_cut_operator)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    _no_store(response)
    return await _execute_web_approval(
        proposal_id=proposal_id,
        action="deny",
        user=user,
        idempotency_key=_require_idempotency_key(idempotency_key),
    )


@router.post("/approvals/{proposal_id}/loss-cut-confirm")
async def confirm_web_loss_cut(
    proposal_id: uuid.UUID,
    response: Response,
    user: Annotated[User, Depends(require_loss_cut_operator)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> dict:
    _no_store(response)
    return await _execute_web_approval(
        proposal_id=proposal_id,
        action="loss-cut-confirm",
        user=user,
        idempotency_key=_require_idempotency_key(idempotency_key),
    )


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
