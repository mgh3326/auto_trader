"""Pure TradingAgents pre-proposal synthesis policy.

This module has no broker/order/watch imports or side effects. It only combines a
candidate with advisory evidence into proposal-ready payload fields.
"""

from __future__ import annotations

from typing import Any

from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
    SynthesizedProposal,
)

_BEARISH_ACTIONS = {
    "underweight",
    "sell",
    "avoid",
    "reduce",
    "reduce_exposure",
}


def synthesize_candidate_with_advisory(
    candidate: CandidateAnalysis,
    advisory: AdvisoryEvidence,
) -> SynthesizedProposal:
    """Reflect TradingAgents advisory evidence before proposal persistence."""

    action = advisory.normalized_action
    conflict = False
    applied_policies: list[str] = []
    final_kind = candidate.proposal_kind
    final_side = candidate.side
    final_confidence = candidate.confidence

    if candidate.side == "buy" and action in _BEARISH_ACTIONS:
        conflict = True
        final_kind = "pullback_watch"
        final_side = "none"
        final_confidence = min(candidate.confidence, 25)
        applied_policies.append("downgrade_buy_on_bearish_advisory")
    elif candidate.side == "buy" and action in {"hold", "neutral"}:
        final_confidence = min(candidate.confidence, 50)
        applied_policies.append("lower_confidence_on_neutral_advisory")
    else:
        applied_policies.append("retain_candidate_with_advisory_evidence")

    if advisory.risk_flags or advisory.warnings:
        lowered = max(0, final_confidence - 10)
        if lowered != final_confidence:
            applied_policies.append("lower_confidence_on_advisory_risk_flags")
            final_confidence = lowered

    evidence_summary = _build_evidence_summary(
        candidate, advisory, final_kind, final_side
    )
    original_payload = _build_original_payload(
        candidate=candidate,
        advisory=advisory,
        final_kind=final_kind,
        final_side=final_side,
        final_confidence=final_confidence,
        conflict=conflict,
        applied_policies=applied_policies,
        evidence_summary=evidence_summary,
    )
    return SynthesizedProposal(
        candidate=candidate,
        advisory=advisory,
        final_proposal_kind=final_kind,
        final_side=final_side,
        final_confidence=final_confidence,
        conflict=conflict,
        applied_policies=applied_policies,
        evidence_summary=evidence_summary,
        original_payload=original_payload,
        original_rationale=evidence_summary,
    )


def _build_evidence_summary(
    candidate: CandidateAnalysis,
    advisory: AdvisoryEvidence,
    final_kind: str,
    final_side: str,
) -> str:
    if candidate.side == "buy" and advisory.normalized_action in _BEARISH_ACTIONS:
        return (
            f"TradingAgents {advisory.advisory_action} advisory downgraded "
            f"{candidate.symbol} buy candidate to {final_kind}/{final_side}."
        )
    return (
        f"TradingAgents {advisory.advisory_action} advisory reflected for "
        f"{candidate.symbol}; final proposal {final_kind}/{final_side}."
    )


def _build_original_payload(
    *,
    candidate: CandidateAnalysis,
    advisory: AdvisoryEvidence,
    final_kind: str,
    final_side: str,
    final_confidence: int,
    conflict: bool,
    applied_policies: list[str],
    evidence_summary: str,
) -> dict[str, Any]:
    advisory_dump = advisory.model_dump(mode="json")
    candidate_dump = candidate.model_dump(mode="json")
    payload = {
        "advisory_only": True,
        "execution_allowed": False,
        "synthesis": {
            "auto_trader": candidate_dump,
            "tradingagents": advisory_dump,
            "applied_policies": applied_policies,
            "conflict": conflict,
            "evidence_summary": evidence_summary,
            "final_proposal_kind": final_kind,
            "final_side": final_side,
            "final_confidence": final_confidence,
            "reflected_action": advisory.advisory_action,
        },
    }
    crypto_workflow = candidate.deterministic_payload.get("crypto_paper_workflow")
    if crypto_workflow is not None:
        payload["crypto_paper_workflow"] = crypto_workflow
    return payload


def build_session_synthesis_meta(
    proposals: list[SynthesizedProposal],
) -> dict[str, Any]:
    """Aggregate proposal-level TradingAgents reflection for session brief."""

    return {
        "advisory_only": True,
        "execution_allowed": False,
        "synthesis_meta": {
            "source": "tradingagents_pre_proposal_synthesis",
            "proposal_count": len(proposals),
            "conflict_count": sum(1 for p in proposals if p.conflict),
            "policies": sorted(
                {policy for p in proposals for policy in p.applied_policies}
            ),
        },
    }
