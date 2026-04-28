from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from app.schemas.trading_decision_synthesis import (
    AdvisoryEvidence,
    CandidateAnalysis,
    SynthesizedProposal,
    advisory_from_runner_result,
)
from app.services.trading_decision_synthesis import (
    build_session_synthesis_meta,
    synthesize_candidate_with_advisory,
)


def _candidate_kwargs(**over):
    base = {
        "symbol": "NVDA",
        "instrument_type": "equity_us",
        "side": "buy",
        "confidence": 65,
        "proposal_kind": "enter",
        "rationale": "auto_trader buy signal",
    }
    base.update(over)
    return base


def _advisory_kwargs(**over):
    base = {
        "advisory_only": True,
        "execution_allowed": False,
        "advisory_action": "Underweight",
        "decision_text": "Reduce exposure; macro risk elevated.",
        "final_trade_decision_text": "No execution authorized.",
        "provider": "openai-compatible",
        "model": "gpt-5.5",
        "base_url": "http://127.0.0.1:8796/v1",
        "warnings": ["macro liquidity risk noted"],
        "risk_flags": ["macro_risk"],
        "raw_state_keys": ["market_report"],
        "as_of_date": date(2026, 4, 27),
    }
    base.update(over)
    return base


def test_candidate_rejects_unknown_side():
    with pytest.raises(ValidationError):
        CandidateAnalysis(**_candidate_kwargs(side="strong_buy"))


def test_candidate_clamps_confidence_range():
    with pytest.raises(ValidationError):
        CandidateAnalysis(**_candidate_kwargs(confidence=101))


def test_advisory_pins_advisory_only_literals():
    with pytest.raises(ValidationError):
        AdvisoryEvidence(**_advisory_kwargs(advisory_only=False))
    with pytest.raises(ValidationError):
        AdvisoryEvidence(**_advisory_kwargs(execution_allowed=True))


def test_synthesized_proposal_payload_advisory_only_present():
    syn = SynthesizedProposal(
        candidate=CandidateAnalysis(**_candidate_kwargs()),
        advisory=AdvisoryEvidence(**_advisory_kwargs()),
        final_proposal_kind="pullback_watch",
        final_side="none",
        final_confidence=25,
        conflict=True,
        applied_policies=["downgrade_buy_on_bearish_advisory"],
        evidence_summary="Downgraded buy → pullback_watch.",
        original_payload={
            "advisory_only": True,
            "execution_allowed": False,
            "synthesis": {"final_proposal_kind": "pullback_watch"},
        },
        original_rationale="Downgraded buy → pullback_watch.",
    )
    assert syn.original_payload["advisory_only"] is True
    assert syn.original_payload["execution_allowed"] is False


def test_buy_candidate_underweight_is_downgraded_to_no_side_watch():
    synthesized = synthesize_candidate_with_advisory(
        CandidateAnalysis(
            **_candidate_kwargs(side="buy", confidence=80, proposal_kind="enter")
        ),
        AdvisoryEvidence(**_advisory_kwargs(advisory_action="Underweight")),
    )

    assert synthesized.conflict is True
    assert synthesized.final_side == "none"
    assert synthesized.final_proposal_kind == "pullback_watch"
    assert synthesized.final_confidence <= 25
    assert "downgrade_buy_on_bearish_advisory" in synthesized.applied_policies
    assert (
        synthesized.original_payload["synthesis"]["tradingagents"]["model"] == "gpt-5.5"
    )
    assert (
        synthesized.original_payload["synthesis"]["tradingagents"]["base_url"]
        == "http://127.0.0.1:8796/v1"
    )
    assert (
        synthesized.original_payload["synthesis"]["reflected_action"] == "Underweight"
    )


def test_agreeing_advisory_retains_candidate_with_evidence():
    synthesized = synthesize_candidate_with_advisory(
        CandidateAnalysis(
            **_candidate_kwargs(side="buy", confidence=70, proposal_kind="enter")
        ),
        AdvisoryEvidence(
            **_advisory_kwargs(advisory_action="Buy", risk_flags=[], warnings=[])
        ),
    )

    assert synthesized.conflict is False
    assert synthesized.final_side == "buy"
    assert synthesized.final_proposal_kind == "enter"
    assert "retain_candidate_with_advisory_evidence" in synthesized.applied_policies


def test_neutral_advisory_lowers_confidence_but_keeps_candidate():
    synthesized = synthesize_candidate_with_advisory(
        CandidateAnalysis(
            **_candidate_kwargs(side="buy", confidence=70, proposal_kind="enter")
        ),
        AdvisoryEvidence(
            **_advisory_kwargs(advisory_action="Hold", risk_flags=[], warnings=[])
        ),
    )

    assert synthesized.conflict is False
    assert synthesized.final_side == "buy"
    assert synthesized.final_confidence <= 50
    assert "lower_confidence_on_neutral_advisory" in synthesized.applied_policies


def test_runner_result_normalization_preserves_metadata_and_invariants():
    evidence = advisory_from_runner_result(
        {
            "decision": "Underweight",
            "advisory_only": True,
            "execution_allowed": False,
            "final_trade_decision": "Avoid until reclaiming resistance.",
            "llm": {
                "provider": "openai-compatible",
                "model": "gpt-5.5",
                "base_url": "http://127.0.0.1:8796/v1",
            },
            "warnings": {"structured_output": ["fallback used"]},
            "risk_flags": ["trend_breakdown"],
            "raw_state_keys": ["market_report"],
            "as_of_date": "2026-04-27",
        }
    )

    assert evidence.advisory_only is True
    assert evidence.execution_allowed is False
    assert evidence.advisory_action == "Underweight"
    assert evidence.model == "gpt-5.5"
    assert str(evidence.base_url) == "http://127.0.0.1:8796/v1"
    assert evidence.warnings == ["fallback used"]


def test_runner_result_normalization_defaults_to_advisory_only_invariants():
    evidence = advisory_from_runner_result(
        {
            "decision": "Underweight",
            "model": "tradingagents-smoke",
            "base_url": "local-smoke",
        }
    )

    assert evidence.advisory_only is True
    assert evidence.execution_allowed is False
    assert evidence.advisory_action == "Underweight"


def test_session_synthesis_meta_counts_conflicts_and_keeps_safety_flags():
    synthesized = synthesize_candidate_with_advisory(
        CandidateAnalysis(**_candidate_kwargs()), AdvisoryEvidence(**_advisory_kwargs())
    )
    meta = build_session_synthesis_meta([synthesized])

    assert meta["advisory_only"] is True
    assert meta["execution_allowed"] is False
    assert meta["synthesis_meta"]["proposal_count"] == 1
    assert meta["synthesis_meta"]["conflict_count"] == 1
