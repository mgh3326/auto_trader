"""Trading decisions API router."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.operator_decision_session import (
    OperatorDecisionRequest,
    OperatorDecisionResponse,
)
from app.schemas.trading_decisions import (
    ActionCreateRequest,
    ActionDetail,
    CounterfactualCreateRequest,
    CounterfactualDetail,
    OutcomeCreateRequest,
    OutcomeDetail,
    ProposalCreateBulkRequest,
    ProposalCreateBulkResponse,
    ProposalDetail,
    ProposalRespondRequest,
    SessionAnalyticsCell,
    SessionAnalyticsResponse,
    SessionCreateRequest,
    SessionDetail,
    SessionListResponse,
    SessionStatusLiteral,
    SessionSummary,
    WorkflowStatusLiteral,
)
from app.services import operator_decision_session_service, trading_decision_service
from app.services.trading_decisions.committee_service import CommitteeSessionService
from app.services.trading_decision_session_url import (
    build_trading_decision_session_url,
    resolve_trading_decision_base_url,
)
from app.services.tradingagents_research_service import (
    TradingAgentsNotConfigured,
    TradingAgentsRunnerError,
)

router = APIRouter(prefix="/trading", tags=["trading-decisions"])


def _to_action_detail(action) -> ActionDetail:
    return ActionDetail(
        id=action.id,
        action_kind=action.action_kind,
        external_order_id=action.external_order_id,
        external_paper_id=action.external_paper_id,
        external_watch_id=action.external_watch_id,
        external_source=action.external_source,
        payload_snapshot=action.payload_snapshot,
        recorded_at=action.recorded_at,
        created_at=action.created_at,
    )


def _to_counterfactual_detail(cf) -> CounterfactualDetail:
    return CounterfactualDetail(
        id=cf.id,
        track_kind=cf.track_kind,
        baseline_price=cf.baseline_price,
        baseline_at=cf.baseline_at,
        quantity=cf.quantity,
        payload=cf.payload,
        notes=cf.notes,
        created_at=cf.created_at,
    )


def _to_outcome_detail(outcome) -> OutcomeDetail:
    return OutcomeDetail(
        id=outcome.id,
        counterfactual_id=outcome.counterfactual_id,
        track_kind=outcome.track_kind,
        horizon=outcome.horizon,
        price_at_mark=outcome.price_at_mark,
        pnl_pct=outcome.pnl_pct,
        pnl_amount=outcome.pnl_amount,
        marked_at=outcome.marked_at,
        payload=outcome.payload,
        created_at=outcome.created_at,
    )


def _to_proposal_detail(proposal) -> ProposalDetail:
    return ProposalDetail(
        proposal_uuid=proposal.proposal_uuid,
        symbol=proposal.symbol,
        instrument_type=proposal.instrument_type,
        proposal_kind=proposal.proposal_kind,
        side=proposal.side,
        user_response=proposal.user_response,
        responded_at=proposal.responded_at,
        created_at=proposal.created_at,
        updated_at=proposal.updated_at,
        original_quantity=proposal.original_quantity,
        original_quantity_pct=proposal.original_quantity_pct,
        original_amount=proposal.original_amount,
        original_price=proposal.original_price,
        original_trigger_price=proposal.original_trigger_price,
        original_threshold_pct=proposal.original_threshold_pct,
        original_currency=proposal.original_currency,
        original_rationale=proposal.original_rationale,
        original_payload=proposal.original_payload,
        user_quantity=proposal.user_quantity,
        user_quantity_pct=proposal.user_quantity_pct,
        user_amount=proposal.user_amount,
        user_price=proposal.user_price,
        user_trigger_price=proposal.user_trigger_price,
        user_threshold_pct=proposal.user_threshold_pct,
        user_note=proposal.user_note,
        actions=[_to_action_detail(a) for a in proposal.actions],
        counterfactuals=[
            _to_counterfactual_detail(c) for c in proposal.counterfactuals
        ],
        outcomes=[_to_outcome_detail(o) for o in proposal.outcomes],
    )


def _to_session_summary(
    session, proposals_count: int, pending_count: int
) -> SessionSummary:
    return SessionSummary(
        session_uuid=session.session_uuid,
        source_profile=session.source_profile,
        strategy_name=session.strategy_name,
        market_scope=session.market_scope,
        status=session.status,
        workflow_status=session.workflow_status,
        account_mode=session.account_mode,
        generated_at=session.generated_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        proposals_count=proposals_count,
        pending_count=pending_count,
    )


def _to_session_detail(session) -> SessionDetail:
    return SessionDetail(
        session_uuid=session.session_uuid,
        source_profile=session.source_profile,
        strategy_name=session.strategy_name,
        market_scope=session.market_scope,
        status=session.status,
        workflow_status=session.workflow_status,
        account_mode=session.account_mode,
        generated_at=session.generated_at,
        created_at=session.created_at,
        updated_at=session.updated_at,
        market_brief=session.market_brief,
        notes=session.notes,
        automation=session.automation,
        artifacts=session.artifacts,
        proposals_count=len(session.proposals),
        pending_count=sum(1 for p in session.proposals if p.user_response == "pending"),
        proposals=[_to_proposal_detail(p) for p in session.proposals],
    )


@router.get("/api/decisions", response_model=SessionListResponse)
async def list_decisions(
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
    status: SessionStatusLiteral | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionListResponse:
    rows, total = await trading_decision_service.list_user_sessions(
        db,
        user_id=current_user.id,
        limit=limit,
        offset=offset,
        status=status,
    )

    sessions = [
        _to_session_summary(session, count, pending) for session, count, pending in rows
    ]

    return SessionListResponse(
        sessions=sessions,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/api/decisions", response_model=SessionDetail, status_code=status.HTTP_201_CREATED
)
async def create_decision(
    request: SessionCreateRequest,
    response: Response,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionDetail:
    session_obj = await trading_decision_service.create_decision_session(
        db,
        user_id=current_user.id,
        source_profile=request.source_profile,
        strategy_name=request.strategy_name,
        market_scope=request.market_scope,
        market_brief=request.market_brief,
        generated_at=request.generated_at,
        notes=request.notes,
        workflow_status=request.workflow_status,
        account_mode=request.account_mode,
        automation=request.automation.model_dump() if request.automation else None,
    )
    await db.commit()

    session_obj = await trading_decision_service.get_session_by_uuid(
        db, session_uuid=session_obj.session_uuid, user_id=current_user.id
    )
    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Decision session not found",
        )
    response.headers["Location"] = f"/trading/api/decisions/{session_obj.session_uuid}"

    return _to_session_detail(session_obj)


@router.get("/api/decisions/{session_uuid}", response_model=SessionDetail)
async def get_decision(
    session_uuid: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionDetail:
    session_obj = await trading_decision_service.get_session_by_uuid(
        db, session_uuid=session_uuid, user_id=current_user.id
    )

    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Decision session not found",
        )

    return _to_session_detail(session_obj)


@router.patch("/api/decisions/{session_uuid}/workflow", response_model=SessionDetail)
async def update_session_workflow(
    session_uuid: UUID,
    status_update: WorkflowStatusLiteral,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionDetail:
    from app.models.trading_decision import WorkflowStatus

    session_obj = await CommitteeSessionService.update_workflow_status(
        db,
        session_uuid=session_uuid,
        user_id=current_user.id,
        status=WorkflowStatus(status_update),
    )
    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Decision session not found",
        )
    await db.commit()

    # Re-fetch to get relationships
    refetched = await trading_decision_service.get_session_by_uuid(
        db, session_uuid=session_uuid, user_id=current_user.id
    )
    return _to_session_detail(refetched)


@router.patch("/api/decisions/{session_uuid}/artifacts", response_model=SessionDetail)
async def update_session_artifacts(
    session_uuid: UUID,
    artifacts_patch: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionDetail:
    session_obj = await CommitteeSessionService.update_committee_artifacts(
        db,
        session_uuid=session_uuid,
        user_id=current_user.id,
        artifacts_patch=artifacts_patch,
    )
    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Decision session not found",
        )
    await db.commit()

    # Re-fetch
    refetched = await trading_decision_service.get_session_by_uuid(
        db, session_uuid=session_uuid, user_id=current_user.id
    )
    return _to_session_detail(refetched)


@router.post(
    "/api/decisions/{session_uuid}/proposals",
    response_model=ProposalCreateBulkResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_proposals(
    session_uuid: UUID,
    request: ProposalCreateBulkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> ProposalCreateBulkResponse:
    session_obj = await trading_decision_service.get_session_by_uuid(
        db, session_uuid=session_uuid, user_id=current_user.id
    )

    if session_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Decision session not found",
        )

    if session_obj.status != "open":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is not open",
        )

    from app.services.trading_decision_service import ProposalCreate

    proposals_to_create = [
        ProposalCreate(
            symbol=p.symbol,
            instrument_type=p.instrument_type,
            proposal_kind=p.proposal_kind,
            side=p.side,
            original_quantity=p.original_quantity,
            original_quantity_pct=p.original_quantity_pct,
            original_amount=p.original_amount,
            original_price=p.original_price,
            original_trigger_price=p.original_trigger_price,
            original_threshold_pct=p.original_threshold_pct,
            original_currency=p.original_currency,
            original_rationale=p.original_rationale,
            original_payload=p.original_payload,
        )
        for p in request.proposals
    ]

    proposals = await trading_decision_service.add_decision_proposals(
        db, session_id=session_obj.id, proposals=proposals_to_create
    )
    await db.commit()

    # Refetch through the ownership-checked read helper so async SQLAlchemy never
    # lazy-loads relationship collections while serializing the response.
    proposal_details = []
    for proposal in proposals:
        refreshed = await trading_decision_service.get_proposal_by_uuid(
            db,
            proposal_uuid=proposal.proposal_uuid,
            user_id=current_user.id,
        )
        if refreshed is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Proposal not found",
            )
        proposal_details.append(_to_proposal_detail(refreshed))

    return ProposalCreateBulkResponse(proposals=proposal_details)


@router.post("/api/proposals/{proposal_uuid}/respond", response_model=ProposalDetail)
async def respond_to_proposal(
    proposal_uuid: UUID,
    request: ProposalRespondRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> ProposalDetail:
    proposal = await trading_decision_service.get_proposal_by_uuid(
        db, proposal_uuid=proposal_uuid, user_id=current_user.id
    )

    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proposal not found",
        )

    if proposal.session.status == "archived":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Session is archived",
        )

    from app.models.trading_decision import UserResponse

    await trading_decision_service.record_user_response(
        db,
        proposal_id=proposal.id,
        response=UserResponse(request.response),
        user_quantity=request.user_quantity,
        user_quantity_pct=request.user_quantity_pct,
        user_amount=request.user_amount,
        user_price=request.user_price,
        user_trigger_price=request.user_trigger_price,
        user_threshold_pct=request.user_threshold_pct,
        user_note=request.user_note,
        responded_at=datetime.now(UTC),
    )
    await db.commit()

    refreshed = await trading_decision_service.get_proposal_by_uuid(
        db, proposal_uuid=proposal_uuid, user_id=current_user.id
    )

    return _to_proposal_detail(refreshed)


@router.post(
    "/api/proposals/{proposal_uuid}/actions",
    response_model=ActionDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_action(
    proposal_uuid: UUID,
    request: ActionCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> ActionDetail:
    proposal = await trading_decision_service.get_proposal_by_uuid(
        db, proposal_uuid=proposal_uuid, user_id=current_user.id
    )

    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proposal not found",
        )

    from app.models.trading_decision import ActionKind

    try:
        action = await trading_decision_service.record_decision_action(
            db,
            proposal_id=proposal.id,
            action_kind=ActionKind(request.action_kind),
            external_order_id=request.external_order_id,
            external_paper_id=request.external_paper_id,
            external_watch_id=request.external_watch_id,
            external_source=request.external_source,
            payload_snapshot=request.payload_snapshot,
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    await db.commit()

    return _to_action_detail(action)


@router.post(
    "/api/proposals/{proposal_uuid}/counterfactuals",
    response_model=CounterfactualDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_counterfactual(
    proposal_uuid: UUID,
    request: CounterfactualCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> CounterfactualDetail:
    proposal = await trading_decision_service.get_proposal_by_uuid(
        db, proposal_uuid=proposal_uuid, user_id=current_user.id
    )

    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proposal not found",
        )

    from app.models.trading_decision import TrackKind

    cf = await trading_decision_service.create_counterfactual_track(
        db,
        proposal_id=proposal.id,
        track_kind=TrackKind(request.track_kind),
        baseline_price=request.baseline_price,
        baseline_at=request.baseline_at,
        quantity=request.quantity,
        payload=request.payload,
        notes=request.notes,
    )
    await db.commit()

    return _to_counterfactual_detail(cf)


@router.post(
    "/api/proposals/{proposal_uuid}/outcomes",
    response_model=OutcomeDetail,
    status_code=status.HTTP_201_CREATED,
)
async def create_outcome(
    proposal_uuid: UUID,
    request: OutcomeCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> OutcomeDetail:
    proposal = await trading_decision_service.get_proposal_by_uuid(
        db, proposal_uuid=proposal_uuid, user_id=current_user.id
    )

    if proposal is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Proposal not found",
        )

    # Verify counterfactual belongs to this proposal if provided
    if request.counterfactual_id is not None:
        cf_ids = [cf.id for cf in proposal.counterfactuals]
        if request.counterfactual_id not in cf_ids:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="counterfactual_id does not belong to this proposal",
            )

    from app.models.trading_decision import OutcomeHorizon, TrackKind

    try:
        outcome = await trading_decision_service.record_outcome_mark(
            db,
            proposal_id=proposal.id,
            track_kind=TrackKind(request.track_kind),
            horizon=OutcomeHorizon(request.horizon),
            price_at_mark=request.price_at_mark,
            counterfactual_id=request.counterfactual_id,
            pnl_pct=request.pnl_pct,
            pnl_amount=request.pnl_amount,
            marked_at=request.marked_at,
            payload=request.payload,
        )
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Outcome mark already exists for this horizon",
        )
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(e),
        )

    return _to_outcome_detail(outcome)


@router.get(
    "/api/decisions/{session_uuid}/analytics",
    response_model=SessionAnalyticsResponse,
)
async def get_session_analytics(
    session_uuid: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> SessionAnalyticsResponse:
    cells = await trading_decision_service.aggregate_session_outcomes(
        db, session_uuid=session_uuid, user_id=current_user.id
    )
    if cells is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found",
        )
    return SessionAnalyticsResponse(
        session_uuid=session_uuid,
        generated_at=datetime.now(UTC),
        tracks=[
            "accepted_live",
            "accepted_paper",
            "rejected_counterfactual",
            "analyst_alternative",
            "user_alternative",
        ],
        horizons=["1h", "4h", "1d", "3d", "7d", "final"],
        cells=[
            SessionAnalyticsCell(
                track_kind=c.track_kind,
                horizon=c.horizon,
                outcome_count=c.outcome_count,
                proposal_count=c.proposal_count,
                mean_pnl_pct=c.mean_pnl_pct,
                sum_pnl_amount=c.sum_pnl_amount,
                latest_marked_at=c.latest_marked_at,
            )
            for c in cells
        ],
    )


@router.post(
    "/api/decisions/from-operator-request",
    response_model=OperatorDecisionResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_decision_from_operator_request(
    payload: OperatorDecisionRequest,
    response: Response,
    fastapi_request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
) -> OperatorDecisionResponse:
    try:
        result = (
            await operator_decision_session_service.create_operator_decision_session(
                db,
                user_id=current_user.id,
                request=payload,
            )
        )
    except TradingAgentsNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="tradingagents_not_configured",
        ) from exc
    except TradingAgentsRunnerError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="tradingagents_runner_failed",
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

    return OperatorDecisionResponse(
        session_uuid=result.session.session_uuid,
        session_url=session_url,
        status=result.session.status,
        proposal_count=result.proposal_count,
        advisory_used=result.advisory_used,
        advisory_skipped_reason=result.advisory_skipped_reason,
    )
