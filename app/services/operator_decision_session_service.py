"""Operator-driven Trading Decision Session orchestrator."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.trading import InstrumentType
from app.models.trading_decision import ProposalKind, TradingDecisionSession
from app.schemas.operator_decision_session import (
    OperatorCandidate,
    OperatorDecisionRequest,
)
from app.schemas.trading_decision_synthesis import (
    CandidateAnalysis,
    advisory_from_runner_result,
)
from app.services import trading_decision_service
from app.services.trading_decision_synthesis import synthesize_candidate_with_advisory
from app.services.trading_decision_synthesis_persistence import (
    create_synthesized_decision_session,
)
from app.services.tradingagents_research_service import (
    TradingAgentsNotConfigured,
    TradingAgentsRunnerError,
    run_tradingagents_research,
)

__all__ = [
    "OperatorSessionResult",
    "TradingAgentsNotConfigured",
    "TradingAgentsRunnerError",
    "create_operator_decision_session",
]


@dataclass(frozen=True)
class OperatorSessionResult:
    session: TradingDecisionSession
    proposal_count: int
    advisory_used: bool
    advisory_skipped_reason: str | None


def _build_no_advisory_proposal(
    candidate: OperatorCandidate,
    *,
    source_profile: str,
) -> dict[str, Any]:
    original_payload: dict[str, Any] = {
        "advisory_only": True,
        "execution_allowed": False,
        "operator_request": {
            "source_profile": source_profile,
            "applied_policies": ["no_advisory"],
            "candidate": candidate.model_dump(mode="json"),
        },
    }
    workflow = _candidate_crypto_paper_workflow(candidate)
    if workflow is not None:
        original_payload["crypto_paper_workflow"] = _serialize_crypto_paper_workflow(
            workflow
        )
    return {
        "symbol": candidate.symbol,
        "instrument_type": InstrumentType(candidate.instrument_type),
        "proposal_kind": ProposalKind(candidate.proposal_kind),
        "side": candidate.side,
        "original_quantity": candidate.quantity,
        "original_quantity_pct": candidate.quantity_pct,
        "original_amount": candidate.amount,
        "original_price": candidate.price,
        "original_trigger_price": candidate.trigger_price,
        "original_threshold_pct": candidate.threshold_pct,
        "original_currency": candidate.currency,
        "original_rationale": candidate.rationale,
        "original_payload": original_payload,
    }


def _candidate_crypto_paper_workflow(
    candidate: OperatorCandidate,
) -> dict[str, Any] | None:
    workflow = {
        "signal_symbol": candidate.signal_symbol,
        "signal_venue": candidate.signal_venue,
        "execution_symbol": candidate.execution_symbol,
        "execution_venue": candidate.execution_venue,
        "execution_mode": candidate.execution_mode,
        "execution_asset_class": candidate.execution_asset_class,
        "workflow_stage": candidate.workflow_stage,
        "purpose": candidate.purpose,
        "preview_payload": candidate.preview_payload,
        "approval_copy": candidate.approval_copy,
    }
    if all(value is None for value in workflow.values()):
        return None
    return workflow


def _serialize_crypto_paper_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal_symbol": workflow["signal_symbol"],
        "signal_venue": workflow["signal_venue"],
        "execution_symbol": workflow["execution_symbol"],
        "execution_venue": workflow["execution_venue"],
        "asset_class": workflow["execution_asset_class"],
        "execution_mode": workflow["execution_mode"],
        "stage": workflow["workflow_stage"],
        "purpose": workflow["purpose"],
        "preview_payload": workflow["preview_payload"],
        "approval_copy": workflow["approval_copy"],
    }


def _build_no_advisory_market_brief(request: OperatorDecisionRequest) -> dict[str, Any]:
    market_brief: dict[str, Any] = {
        "advisory_only": True,
        "execution_allowed": False,
        "operator_request": {
            "applied_policies": ["no_advisory"],
            "include_tradingagents": False,
        },
    }
    crypto_workflows = [
        workflow
        for candidate in request.candidates
        if (workflow := _candidate_crypto_paper_workflow(candidate)) is not None
    ]
    if request.market_scope == "crypto" and crypto_workflows:
        first_workflow = crypto_workflows[0]
        market_brief["stage"] = first_workflow.get("workflow_stage")
        market_brief["crypto_paper_workflow"] = {
            "stage": first_workflow.get("workflow_stage"),
            "workflow_count": len(crypto_workflows),
            "signal_venue": first_workflow.get("signal_venue"),
            "execution_venue": first_workflow.get("execution_venue"),
            "execution_mode": first_workflow.get("execution_mode"),
        }
    return market_brief


def _build_candidate_analysis(candidate: OperatorCandidate) -> CandidateAnalysis:
    deterministic_payload: dict[str, Any] = {}
    workflow = _candidate_crypto_paper_workflow(candidate)
    if workflow is not None:
        deterministic_payload["crypto_paper_workflow"] = (
            _serialize_crypto_paper_workflow(workflow)
        )
    return CandidateAnalysis(
        symbol=candidate.symbol,
        instrument_type=candidate.instrument_type,
        side=candidate.side,
        confidence=candidate.confidence,
        proposal_kind=candidate.proposal_kind,
        rationale=candidate.rationale,
        quantity=candidate.quantity,
        quantity_pct=candidate.quantity_pct,
        amount=candidate.amount,
        price=candidate.price,
        trigger_price=candidate.trigger_price,
        threshold_pct=candidate.threshold_pct,
        currency=candidate.currency,
        deterministic_payload=deterministic_payload,
    )


async def create_operator_decision_session(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OperatorSessionResult:
    generated_at = request.generated_at or now()
    if request.include_tradingagents:
        return await _run_with_advisory(
            db, user_id=user_id, request=request, generated_at=generated_at
        )
    return await _run_without_advisory(
        db, user_id=user_id, request=request, generated_at=generated_at
    )


async def _run_without_advisory(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    generated_at: datetime,
) -> OperatorSessionResult:
    session_obj = await trading_decision_service.create_decision_session(
        db,
        user_id=user_id,
        source_profile=request.source_profile,
        strategy_name=request.strategy_name,
        market_scope=request.market_scope,
        market_brief=_build_no_advisory_market_brief(request),
        generated_at=generated_at,
        notes=request.notes,
    )
    proposals = await trading_decision_service.add_decision_proposals(
        db,
        session_id=session_obj.id,
        proposals=[
            _build_no_advisory_proposal(c, source_profile=request.source_profile)
            for c in request.candidates
        ],
    )
    return OperatorSessionResult(
        session=session_obj,
        proposal_count=len(proposals),
        advisory_used=False,
        advisory_skipped_reason="include_tradingagents=False",
    )


async def _run_with_advisory(
    db: AsyncSession,
    *,
    user_id: int,
    request: OperatorDecisionRequest,
    generated_at: datetime,
) -> OperatorSessionResult:
    synthesized_proposals = []
    as_of: date = generated_at.astimezone(UTC).date()
    for candidate in request.candidates:
        runner_result = await run_tradingagents_research(
            symbol=candidate.symbol,
            instrument_type=InstrumentType(candidate.instrument_type),
            as_of_date=as_of,
            analysts=request.analysts,
        )
        advisory = advisory_from_runner_result(runner_result.model_dump(mode="json"))
        synthesized_proposals.append(
            synthesize_candidate_with_advisory(
                _build_candidate_analysis(candidate), advisory
            )
        )

    session_obj, db_proposals = await create_synthesized_decision_session(
        db,
        user_id=user_id,
        proposals=synthesized_proposals,
        generated_at=generated_at,
        source_profile=f"{request.source_profile}+tradingagents",
        strategy_name=request.strategy_name,
        market_scope=request.market_scope,
        market_brief={
            "advisory_only": True,
            "execution_allowed": False,
            "operator_request": {
                "include_tradingagents": True,
                "analysts": list(request.analysts) if request.analysts else None,
            },
        },
        notes=request.notes,
    )
    return OperatorSessionResult(
        session=session_obj,
        proposal_count=len(db_proposals),
        advisory_used=True,
        advisory_skipped_reason=None,
    )
