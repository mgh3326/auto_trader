"""Shared test builders for preopen approval bridge tests."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from app.schemas.preopen import (
    CandidateSummary,
    PreopenBriefingArtifact,
    PreopenDecisionSessionCta,
    PreopenQaCheck,
    PreopenQaEvaluatorSummary,
    PreopenQaScore,
)


def preopen_candidate(
    *,
    symbol: str,
    instrument_type: str,
    side: str = "buy",
    price: Decimal | None = None,
    quantity: Decimal | None = None,
    currency: str = "KRW",
    rationale: str = "Preopen approval bridge candidate",
    **overrides,
) -> CandidateSummary:
    payload = {
        "candidate_uuid": uuid4(),
        "symbol": symbol,
        "instrument_type": instrument_type,
        "side": side,
        "candidate_kind": "proposed",
        "proposed_price": price,
        "proposed_qty": quantity,
        "confidence": 70,
        "rationale": rationale,
        "currency": currency,
        "warnings": [],
    }
    payload.update(overrides)
    return CandidateSummary(**payload)


def preopen_artifact(market_scope: str, **overrides) -> PreopenBriefingArtifact:
    payload = {
        "status": "ready",
        "market_scope": market_scope,
        "stage": "preopen",
        "risk_notes": [],
        "cta": PreopenDecisionSessionCta(
            state="create_available",
            label="Create decision session",
            requires_confirmation=True,
        ),
        "qa": {"read_only": True},
    }
    payload.update(overrides)
    return PreopenBriefingArtifact(**payload)


def preopen_qa(
    summary: str = "Execution disabled.", **overrides
) -> PreopenQaEvaluatorSummary:
    payload = {
        "status": "ready",
        "generated_at": datetime.now(UTC),
        "overall": PreopenQaScore(score=90, grade="excellent", confidence="high"),
        "checks": [
            PreopenQaCheck(
                id="actionability_guardrail",
                label="Actionability guardrail",
                status="pass",
                severity="info",
                summary=summary,
                details={"advisory_only": True, "execution_allowed": False},
            )
        ],
        "blocking_reasons": [],
        "warnings": [],
        "coverage": {"advisory_only": True, "execution_allowed": False},
    }
    payload.update(overrides)
    return PreopenQaEvaluatorSummary(**payload)
