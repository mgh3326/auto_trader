from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.buy_gate_ab_shadow.evaluate import (
    CandidateEvidence,
    EvaluationError,
    evaluate_candidate,
    evaluate_candidates,
)

_AS_OF = datetime(2026, 8, 20, 6, 30, tzinfo=UTC)


def _row(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "005930",
        "market": "kr",
        "current_price": "70000",
        "support_strength": "moderate",
        "support_distance_pct": "4",
        "rsi": "40",
        "honest_upside_pct": "45",
        "other_gate_bits": {
            "liquid_midcap": True,
            "concentration": True,
            "overhang": True,
        },
    }
    payload.update(overrides)
    return payload


def test_moderate_support_is_b_only_when_shared_gates_pass() -> None:
    result = evaluate_candidate(
        CandidateEvidence.from_mapping(_row()),
        evaluation_as_of=_AS_OF,
    )
    assert result.variant_a.passed is False
    assert result.variant_b.passed is True
    assert result.cohort == "b_only"
    assert result.shadow_buy is True
    assert "support_strength_below_strong" in result.variant_a.reject_reasons
    assert result.variant_a.reject_reasons == result.shared_reject_reasons + (
        "support_strength_below_strong",
    )
    assert result.variant_b.reject_reasons == ()
    assert result.entry_price == Decimal("70000")
    assert result.evaluation_as_of == _AS_OF


def test_strong_support_passes_both_and_is_not_shadow_buy() -> None:
    result = evaluate_candidate(
        CandidateEvidence.from_mapping(_row(support_strength="strong")),
        evaluation_as_of=_AS_OF,
    )
    assert result.variant_a.passed is True
    assert result.variant_b.passed is True
    assert result.cohort == "a_and_b"
    assert result.shadow_buy is False


def test_weak_support_fails_both() -> None:
    result = evaluate_candidate(
        CandidateEvidence.from_mapping(_row(support_strength="weak")),
        evaluation_as_of=_AS_OF,
    )
    assert result.cohort == "neither"
    assert result.shadow_buy is False
    assert "support_strength_below_strong" in result.variant_a.reject_reasons
    assert "support_strength_below_moderate" in result.variant_b.reject_reasons


def test_shared_gate_failure_is_identical_on_both_variants() -> None:
    result = evaluate_candidate(
        CandidateEvidence.from_mapping(_row(rsi="50", support_strength="strong")),
        evaluation_as_of=_AS_OF,
    )
    assert result.variant_a.passed is False
    assert result.variant_b.passed is False
    assert result.variant_a.reject_reasons == result.variant_b.reject_reasons
    assert result.variant_a.reject_reasons == ("rsi_not_below_max",)


def test_missing_other_gate_bit_fails_closed_for_both() -> None:
    result = evaluate_candidate(
        CandidateEvidence.from_mapping(
            _row(other_gate_bits={"liquid_midcap": True, "concentration": True})
        ),
        evaluation_as_of=_AS_OF,
    )
    assert result.cohort == "neither"
    assert "other_gate_overhang_failed" in result.shared_reject_reasons
    assert result.variant_a.passed is False
    assert result.variant_b.passed is False
    assert result.shared_reject_reasons == ("other_gate_overhang_failed",)
    assert result.shared_reject_reasons == tuple(
        reason
        for reason in result.variant_a.reject_reasons
        if not reason.startswith("support_strength_below_")
    )
    assert result.shared_reject_reasons == tuple(
        reason
        for reason in result.variant_b.reject_reasons
        if not reason.startswith("support_strength_below_")
    )


def test_same_snapshot_is_the_only_input_to_both_variants() -> None:
    evidence = CandidateEvidence.from_mapping(_row())
    result = evaluate_candidate(evidence, evaluation_as_of=_AS_OF)
    assert result.variant_a.support_strength_min == "strong"
    assert result.variant_b.support_strength_min == "moderate"
    dumped = result.as_dict()
    assert dumped["live_gate_impact"] is False
    assert dumped["promote"] is False
    assert dumped["input_snapshot"] == evidence.input_snapshot()
    assert dumped["input_snapshot_sha256"] == evidence.input_snapshot_sha256()
    assert len(dumped["input_snapshot_sha256"]) == 64


def test_non_boolean_other_gate_bit_is_rejected_not_truthified() -> None:
    with pytest.raises(EvaluationError, match="other_gate_bits.overhang"):
        CandidateEvidence.from_mapping(
            _row(
                other_gate_bits={
                    "liquid_midcap": True,
                    "concentration": True,
                    "overhang": "false",
                }
            )
        )


def test_naive_as_of_is_rejected() -> None:
    with pytest.raises(EvaluationError, match="timezone-aware"):
        evaluate_candidate(
            CandidateEvidence.from_mapping(_row()),
            evaluation_as_of=datetime(2026, 8, 20, 6, 30),
        )


def test_crypto_market_rejected() -> None:
    with pytest.raises(EvaluationError, match="kr or us"):
        CandidateEvidence.from_mapping(_row(market="crypto"))


def test_batch_shares_one_evaluation_as_of() -> None:
    rows = evaluate_candidates(
        [_row(), _row(symbol="AAPL", market="us", current_price="150")],
        evaluation_as_of=_AS_OF,
    )
    assert {row.evaluation_as_of for row in rows} == {_AS_OF}
    assert [row.symbol for row in rows] == ["005930", "AAPL"]
