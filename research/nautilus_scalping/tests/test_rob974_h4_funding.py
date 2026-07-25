import math

import pytest
from rob974_h4_selection import (
    PITFundingObservation,
    invoke_after_arbitration,
    last_known_pit_funding,
    s3_funding_gate,
    s4_funding_gate,
)


def test_s3_exact_three_bps_and_credit_pass_but_next_ulp_rejects() -> None:
    assert s3_funding_gate(3.0).accepted
    assert s3_funding_gate(-3.0).accepted
    assert (
        s3_funding_gate(math.nextafter(3.0, math.inf)).reason
        == "expected_funding_cost_above_3bps"
    )


def test_missing_malformed_and_last_known_pit_funding_fail_closed() -> None:
    assert s3_funding_gate(None).reason == "funding_evidence_unavailable"
    with pytest.raises(TypeError):
        s3_funding_gate(3)
    evidence = (PITFundingObservation(1_000, 1.0), PITFundingObservation(2_000, 2.0))
    assert last_known_pit_funding(observations=evidence, entry_ts=1_500) == 1.0
    assert last_known_pit_funding(observations=evidence, entry_ts=999) is None


def test_s4_requires_both_legs_and_uses_entry_frozen_weights_once() -> None:
    assert (
        s4_funding_gate(
            leg_a_signed_bps=None, leg_b_signed_bps=1.0, weight_a=0.4, weight_b=0.6
        ).reason
        == "funding_evidence_unavailable"
    )
    result = s4_funding_gate(
        leg_a_signed_bps=4.0, leg_b_signed_bps=2.0, weight_a=0.4, weight_b=0.6
    )
    assert result.accepted and result.expected_signed_bps == 2.8
    assert (
        s4_funding_gate(
            leg_a_signed_bps=4.0, leg_b_signed_bps=4.0, weight_a=0.4, weight_b=0.6
        ).reason
        == "expected_funding_cost_above_3bps"
    )


def test_funding_rejection_never_falls_back_or_calls_h2_open() -> None:
    calls: list[str] = []

    def entry(winner: str) -> str:
        calls.append(f"entry:{winner}")
        return "exact"

    def reject(winner: str, resolved: str):
        calls.append(f"funding:{winner}:{resolved}")
        return s3_funding_gate(4.0)

    def h2_open(winner: str, resolved: str) -> str:
        calls.append(f"open:{winner}:{resolved}")
        return "opened"

    status, gate, opened = invoke_after_arbitration(
        winner="global-winner",
        resolve_exact_entry=entry,
        funding_gate=reject,
        h2_open=h2_open,
    )
    assert (status, gate.reason if gate else None, opened) == (
        "expected_funding_cost_above_3bps",
        "expected_funding_cost_above_3bps",
        None,
    )
    assert calls == ["entry:global-winner", "funding:global-winner:exact"]
