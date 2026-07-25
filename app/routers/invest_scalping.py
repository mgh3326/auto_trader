"""ROB-315 Phase 2 — read/review `/invest/api/scalping` surface.

Thin router over ``ScalpingReviewService``. It builds/reads daily review
drafts and records operator judgment + actions. It imports **no** broker /
order / scheduler / market-data module and reaches no mutation path on any
venue — it only reads ``scalp_trade_analytics`` (via the service) and writes
the two review tables. This boundary is asserted by a static import-guard test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.models.scalping_reviews import ScalpingDailyReview, ScalpingReviewAction
from app.routers.dependencies import get_authenticated_user
from app.schemas.invest_scalping import (
    Product,
    ScalpingActionCreateRequest,
    ScalpingActionPatchRequest,
    ScalpingDraftRequest,
    ScalpingReviewPatchRequest,
)
from app.services.scalping_reviews.service import (
    ScalpingReviewError,
    ScalpingReviewService,
)

router = APIRouter(prefix="/invest/api/scalping", tags=["invest", "scalping"])


def get_scalping_review_service(
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ScalpingReviewService:
    return ScalpingReviewService(db)


_SVC = Annotated[ScalpingReviewService, Depends(get_scalping_review_service)]
_USER = Annotated[Any, Depends(get_authenticated_user)]


def _num(value: Decimal | None) -> str | None:
    """Serialize a Decimal as a string to preserve precision (n/a → null)."""
    return None if value is None else str(value)


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _serialize_action(action: ScalpingReviewAction) -> dict[str, Any]:
    return {
        "id": action.id,
        "reviewId": action.review_id,
        "actionType": action.action_type,
        "title": action.title,
        "rationale": action.rationale,
        "targetComponent": action.target_component,
        "proposedChange": action.proposed_change,
        "expectedEffect": action.expected_effect,
        "status": action.status,
        "createdAt": _iso(action.created_at),
        "updatedAt": _iso(action.updated_at),
    }


def _serialize_review(review: ScalpingDailyReview) -> dict[str, Any]:
    return {
        "id": review.id,
        "reviewDate": review.review_date.isoformat(),
        "product": review.product,
        "accountScope": review.account_scope,
        "sessionTag": review.session_tag,
        "metrics": {
            "tradeCount": review.trade_count,
            "winCount": review.win_count,
            "lossCount": review.loss_count,
            "anomalyCount": review.anomaly_count,
            "grossPnlUsdt": _num(review.gross_pnl_usdt),
            "netPnlUsdt": _num(review.net_pnl_usdt),
            "netReturnBps": _num(review.net_return_bps),
            "benchmarkReturnBps": _num(review.benchmark_return_bps),
            "avgSlippageBps": _num(review.avg_slippage_bps),
            "avgSpreadBps": _num(review.avg_spread_bps),
            "avgMaeBps": _num(review.avg_mae_bps),
            "avgMfeBps": _num(review.avg_mfe_bps),
            "avgHoldingSeconds": review.avg_holding_seconds,
            "exitReasonCounts": review.exit_reason_counts or {},
        },
        "observation": review.observation,
        "rootCause": review.root_cause,
        "improvement": review.improvement,
        "nextRunPlan": review.next_run_plan,
        "decision": review.decision,
        "status": review.status,
        "sourcePayload": review.source_payload,
        "createdAt": _iso(review.created_at),
        "updatedAt": _iso(review.updated_at),
    }


def _serialize_analytics(row: ScalpTradeAnalytics) -> dict[str, Any]:
    return {
        "id": row.id,
        "openClientOrderId": row.open_client_order_id,
        "symbol": row.symbol,
        "side": row.side,
        "qty": _num(row.qty),
        "entryPrice": _num(row.entry_price),
        "exitPrice": _num(row.exit_price),
        "entrySlippageBps": _num(row.entry_slippage_bps),
        "exitSlippageBps": _num(row.exit_slippage_bps),
        "entrySpreadBps": _num(row.entry_spread_bps),
        "exitSpreadBps": _num(row.exit_spread_bps),
        "maeBps": _num(row.mae_bps),
        "mfeBps": _num(row.mfe_bps),
        "netPnlUsdt": _num(row.net_pnl_usdt),
        "holdingSeconds": row.holding_seconds,
        "exitReason": row.exit_reason,
        # No derivable entry fill price → a partial/anomaly row (ROB-315 0b).
        "isAnomaly": row.entry_price is None,
    }


@router.get("/analytics")
async def list_analytics(
    user: _USER,
    service: _SVC,
    review_date: Annotated[date, Query(alias="date")],
    product: Annotated[Product, Query()],
) -> dict[str, Any]:
    rows = await service.list_analytics(review_date=review_date, product=product)
    return {"items": [_serialize_analytics(r) for r in rows]}


@router.get("/reviews")
async def list_reviews(
    user: _USER,
    service: _SVC,
    review_date: Annotated[date | None, Query(alias="date")] = None,
    product: Annotated[Product | None, Query()] = None,
) -> dict[str, Any]:
    reviews = await service.list_reviews(review_date=review_date, product=product)
    return {"items": [_serialize_review(r) for r in reviews]}


@router.get("/reviews/{review_id}")
async def get_review(user: _USER, service: _SVC, review_id: int) -> dict[str, Any]:
    review = await service.get(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    actions = await service.list_actions(review_id)
    return {
        "review": _serialize_review(review),
        "actions": [_serialize_action(a) for a in actions],
    }


@router.post("/reviews/draft")
async def build_draft(
    user: _USER, service: _SVC, body: ScalpingDraftRequest
) -> dict[str, Any]:
    try:
        review = await service.build_draft(
            review_date=body.review_date,
            product=body.product,
            session_tag=body.session_tag,
            now=datetime.now(UTC),
        )
    except ScalpingReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"review": _serialize_review(review)}


@router.patch("/reviews/{review_id}")
async def patch_review(
    user: _USER,
    service: _SVC,
    review_id: int,
    body: ScalpingReviewPatchRequest,
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    try:
        review = await service.update_review(review_id, now=datetime.now(UTC), **fields)
    except ScalpingReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if review is None:
        raise HTTPException(status_code=404, detail="review not found")
    return {"review": _serialize_review(review)}


@router.post("/reviews/{review_id}/actions")
async def create_action(
    user: _USER,
    service: _SVC,
    review_id: int,
    body: ScalpingActionCreateRequest,
) -> dict[str, Any]:
    if await service.get(review_id) is None:
        raise HTTPException(status_code=404, detail="review not found")
    try:
        action = await service.add_action(
            review_id,
            action_type=body.action_type,
            title=body.title,
            rationale=body.rationale,
            target_component=body.target_component,
            proposed_change=body.proposed_change,
            expected_effect=body.expected_effect,
            now=datetime.now(UTC),
        )
    except ScalpingReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"action": _serialize_action(action)}


@router.patch("/actions/{action_id}")
async def patch_action(
    user: _USER,
    service: _SVC,
    action_id: int,
    body: ScalpingActionPatchRequest,
) -> dict[str, Any]:
    fields = body.model_dump(exclude_unset=True)
    try:
        action = await service.update_action(action_id, now=datetime.now(UTC), **fields)
    except ScalpingReviewError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if action is None:
        raise HTTPException(status_code=404, detail="action not found")
    return {"action": _serialize_action(action)}
