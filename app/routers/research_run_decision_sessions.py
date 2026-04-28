"""Research run decision session API router."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.research_run_decision_session import (
    ResearchRunDecisionSessionRequest,
    ResearchRunDecisionSessionResponse,
)
from app.services import (
    research_run_decision_session_service,
    research_run_live_refresh_service,
)
from app.services.trading_decision_session_url import (
    build_trading_decision_session_url,
    resolve_trading_decision_base_url,
)

LiveRefreshTimeout = getattr(
    research_run_live_refresh_service,
    "LiveRefreshTimeout",
    TimeoutError,
)

router = APIRouter(prefix="/trading", tags=["research-run-decisions"])


@router.post(
    "/api/decisions/from-research-run",
    response_model=ResearchRunDecisionSessionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision_from_research_run(
    payload: ResearchRunDecisionSessionRequest,
    fastapi_request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> ResearchRunDecisionSessionResponse:
    try:
        research_run = await research_run_decision_session_service.resolve_research_run(
            db,
            user_id=current_user.id,
            selector=payload.selector,
        )
        snapshot = await research_run_live_refresh_service.build_live_refresh_snapshot(
            db,
            run=research_run,
            user_id=current_user.id,
        )
        result = await research_run_decision_session_service.create_decision_session_from_research_run(
            db,
            user_id=current_user.id,
            research_run=research_run,
            snapshot=snapshot,
            request=payload,
        )
    except research_run_decision_session_service.ResearchRunNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="research_run_not_found",
        ) from exc
    except research_run_decision_session_service.EmptyResearchRunError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="research_run_has_no_candidates",
        ) from exc
    except LiveRefreshTimeout as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="live_refresh_timeout",
        ) from exc
    except NotImplementedError as exc:
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="not_implemented",
        ) from exc

    await db.commit()

    base_url = resolve_trading_decision_base_url(
        configured=settings.public_base_url,
        request_base_url=str(fastapi_request.base_url),
    )
    session_url = build_trading_decision_session_url(
        base_url=base_url,
        session_uuid=result.session.session_uuid,
    )
    response.headers["Location"] = (
        f"/trading/api/decisions/{result.session.session_uuid}"
    )

    return ResearchRunDecisionSessionResponse(
        session_uuid=result.session.session_uuid,
        session_url=session_url,
        status=result.session.status,
        research_run_uuid=result.research_run.run_uuid,
        refreshed_at=result.refreshed_at,
        proposal_count=result.proposal_count,
        reconciliation_count=result.reconciliation_count,
        advisory_used=False,
        advisory_skipped_reason=(
            "include_tradingagents_not_supported"
            if payload.include_tradingagents
            else None
        ),
        warnings=list(result.warnings),
    )
