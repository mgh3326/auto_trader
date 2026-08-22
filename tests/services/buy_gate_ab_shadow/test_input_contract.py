"""ROB-1315 §5-1 — a mis-keyed candidate is refused, never silently ignored.

On 2026-08-21 a US session sent ``rsi_14`` and ``nearest_support_strength``.
Both keys were dropped, both fields read as absent, all seven candidates were
rejected for gates they would have passed, and the response said nothing. A
collection day produced rows that meant nothing. These tests pin the opposite
behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.mcp_server.tooling.buy_gate_ab_shadow import (
    evaluate_buy_gate_ab_shadow_impl,
)
from app.services.buy_gate_ab_shadow.evaluate import (
    CANDIDATE_KEYS,
    CandidateEvidence,
    EvaluationError,
    candidate_input_contract,
    evaluate_candidates,
)

pytestmark = pytest.mark.unit

_AS_OF = datetime(2026, 8, 21, 0, 0, tzinfo=UTC)


def _row(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "CIEN",
        "market": "us",
        "current_price": "95",
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


def test_the_2026_08_21_typos_are_rejected_and_named() -> None:
    bad = _row()
    bad["rsi_14"] = bad.pop("rsi")
    bad["nearest_support_strength"] = bad.pop("support_strength")

    with pytest.raises(EvaluationError) as exc:
        CandidateEvidence.from_mapping(bad)

    message = str(exc.value)
    assert "rsi_14" in message
    assert "nearest_support_strength" in message
    # the correction, not just the complaint
    assert "rsi_14 -> rsi" in message
    assert "nearest_support_strength -> support_strength" in message


def test_an_unrecognised_key_without_a_known_alias_is_still_rejected() -> None:
    with pytest.raises(EvaluationError) as exc:
        CandidateEvidence.from_mapping(_row(sector="tech"))

    assert "sector" in str(exc.value)


def test_the_failing_row_is_identified_by_index_and_symbol() -> None:
    rows = [_row(), _row(symbol="RDDT", rsi_14="39")]
    rows[1].pop("rsi")

    with pytest.raises(EvaluationError) as exc:
        evaluate_candidates(rows, evaluation_as_of=_AS_OF)

    assert "candidates[1]" in str(exc.value)


def test_unknown_other_gate_bits_key_is_rejected() -> None:
    with pytest.raises(EvaluationError) as exc:
        CandidateEvidence.from_mapping(
            _row(other_gate_bits={"liquid_midcap": True, "overhang_risk": False})
        )

    assert "overhang_risk" in str(exc.value)
    assert "liquid_midcap" in str(exc.value)


def test_an_invalid_support_strength_word_is_rejected() -> None:
    with pytest.raises(EvaluationError) as exc:
        CandidateEvidence.from_mapping(_row(support_strength="firm"))

    assert "firm" in str(exc.value)


def test_omitting_an_optional_field_is_allowed_and_still_rejects_the_gate() -> None:
    """Absent evidence is a rejection — but a deliberate one, not a typo."""

    row = _row()
    del row["rsi"]

    evidence = CandidateEvidence.from_mapping(row)
    result = evaluate_candidates([row], evaluation_as_of=_AS_OF)[0]

    assert evidence.rsi is None
    assert "rsi_not_below_max" in result.variant_b.reject_reasons
    assert result.cohort == "neither"


def test_every_contract_key_is_accepted() -> None:
    evidence = CandidateEvidence.from_mapping(_row())

    assert set(_row()) == CANDIDATE_KEYS
    assert evidence.symbol == "CIEN"


def test_impl_echoes_the_contract_on_a_mis_keyed_call() -> None:
    bad = _row()
    bad["rsi_14"] = bad.pop("rsi")

    result = evaluate_buy_gate_ab_shadow_impl(
        [bad],
        evaluation_as_of=_AS_OF.isoformat(),
        created_by="us-open",
    )

    assert result["success"] is False
    assert result["candidates_evaluated"] == 0
    contract = result["input_contract"]
    assert "rsi" in contract["optional"]
    assert contract["common_mistakes"]["rsi_14"] == "rsi"
    assert contract["unknown_keys_are_rejected"] is True
    # a refused call produces no shadow rows at all
    assert "shadow_buy_forecasts" not in result
    assert result["promote"] is False


def test_impl_echoes_the_contract_on_success_too() -> None:
    result = evaluate_buy_gate_ab_shadow_impl(
        [_row()],
        evaluation_as_of=_AS_OF.isoformat(),
        created_by="us-open",
    )

    assert result["success"] is True
    assert result["input_contract"] == candidate_input_contract()
    assert result["counts"]["n"] == 1


def test_contract_lists_exactly_the_accepted_keys() -> None:
    contract = candidate_input_contract()

    assert set(contract["required"]) | set(contract["optional"]) == CANDIDATE_KEYS
    assert contract["other_gate_bit_keys"] == [
        "liquid_midcap",
        "concentration",
        "overhang",
    ]
    assert contract["omitted_optional_field_is_a_rejection_not_a_pass"] is True
