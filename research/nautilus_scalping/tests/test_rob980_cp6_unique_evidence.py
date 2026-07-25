from __future__ import annotations

import dataclasses
import importlib
import importlib.util
import inspect
import math

import pytest
import rob974_h3_s3 as s3
import rob974_h3_s4 as s4
from rob974_h3_manifest import PAIRS, SYMBOLS, get_config, strategy_contract_payload


@pytest.fixture(scope="module")
def evidence():
    spec = importlib.util.find_spec("rob974_h3_evidence")
    assert spec is not None, "ROB-980 CP6 unique generator evidence is not implemented"
    return importlib.import_module("rob974_h3_evidence")


def _s3_candidate(symbol: str, strength: float):
    config = get_config("S3-00")
    metrics = s3.S3Metrics(
        config.config_id,
        144_000_000,
        symbol,
        0.10,
        0.50,
        strength,
        0.50,
        -0.10,
        101.0,
        100.0,
        102.0,
        90.0,
        0.6,
        0.006,
        100.0,
        99.0,
        50.0,
        0.10,
        0.0075,
        -0.50,
        2,
        1,
    )
    candidate = s3.evaluate_s3_gates(metrics, config).candidate
    assert candidate is not None
    return candidate


def _s3_output():
    candidates = tuple(
        _s3_candidate(symbol, 2.0 - index * 0.1) for index, symbol in enumerate(SYMBOLS)
    )
    arbitration = s3.arbitrate_s3_candidates(candidates[:2])
    accepted = arbitration.winner
    rejected = arbitration.rejected[0]
    decisions = tuple(
        (
            s3.S3UnitDecision(
                candidate.decision_ts,
                candidate.symbol,
                "GENERATOR_ACCEPTED",
                candidate.side,
                candidate,
                None,
                None,
            )
            if candidate.identity == accepted.identity
            else s3.S3UnitDecision(
                candidate.decision_ts,
                candidate.symbol,
                "GENERATOR_REJECTED",
                candidate.side,
                candidate,
                None,
                "simultaneous_candidate_arbitration_loser",
            )
        )
        for candidate in candidates[:2]
    ) + (
        s3.S3UnitDecision(
            144_000_000,
            SYMBOLS[2],
            "NO_SIGNAL",
            "long",
            None,
            "momentum",
            None,
        ),
    )
    return s3.S3GeneratorOutput("S3", "S3-00", decisions, (accepted,), (rejected,))


def _s4_estimate(pair: str, distance: float, z_value: float):
    symbols = {
        "XRP-DOGE": ("XRPUSDT", "DOGEUSDT"),
        "XRP-SOL": ("XRPUSDT", "SOLUSDT"),
        "DOGE-SOL": ("DOGEUSDT", "SOLUSDT"),
    }[pair]
    return s4.S4Estimate(
        "S4-00",
        144_000_000,
        pair,
        symbols[0],
        symbols[1],
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        1.0,
        0.5,
        0.5,
        distance,
        0.0,
        0.01,
        0.014826,
        z_value,
        1.0,
        1.0,
        0.5,
        0.5,
        0.0,
        0.01,
        0.014826,
        math.copysign(max(abs(z_value) / 0.90, 2.0), z_value),
        distance,
        distance * 10_000.0,
        0.70,
        0.75,
        4.0,
        0.10,
        0.0,
        -0.001,
        -10.0,
        0.50,
    )


def _s4_output():
    candidates = tuple(
        s4.evaluate_s4_gates(
            _s4_estimate(pair, 0.020 - index * 0.001, 2.0),
            get_config("S4-00"),
        ).candidate
        for index, pair in enumerate(PAIRS)
    )
    assert all(candidate is not None for candidate in candidates)
    exact = tuple(candidate for candidate in candidates if candidate is not None)
    arbitration = s4.arbitrate_s4_candidates(exact[:2])
    accepted = arbitration.winner
    rejected = arbitration.rejected[0]
    decisions = tuple(
        (
            s4.S4UnitDecision(
                candidate.decision_ts,
                candidate.pair,
                "GENERATOR_ACCEPTED",
                candidate.side,
                candidate,
                None,
                None,
            )
            if candidate.identity == accepted.identity
            else s4.S4UnitDecision(
                candidate.decision_ts,
                candidate.pair,
                "GENERATOR_REJECTED",
                candidate.side,
                candidate,
                None,
                "simultaneous_pair_arbitration_loser",
            )
        )
        for candidate in exact[:2]
    ) + (
        s4.S4UnitDecision(
            144_000_000,
            PAIRS[2],
            "NO_SIGNAL",
            None,
            None,
            "rho",
            None,
        ),
    )
    return s4.S4GeneratorOutput("S4", "S4-00", decisions, (accepted,), (rejected,))


def test_closed_identity_phase_and_scenario_absence(evidence):
    identity = evidence.GeneratorIdentity(
        "S3",
        "S3-00",
        "fold-00",
        "selected_oos",
        144_000_000,
        "XRPUSDT",
        "long",
    )
    assert identity.as_tuple() == (
        "S3",
        "S3-00",
        "fold-00",
        "selected_oos",
        144_000_000,
        "XRPUSDT",
        "long",
    )
    assert evidence.PHASES == (
        "train",
        "selected_oos",
        "pbo_full_window",
        "offline_smoke",
    )
    assert "scenario" not in {field.name for field in dataclasses.fields(identity)}
    with pytest.raises(ValueError):
        dataclasses.replace(identity, phase="base13")
    with pytest.raises(TypeError):
        dataclasses.replace(identity, decision_ts=True)


@pytest.mark.parametrize(
    ("output_factory", "strategy", "config_id", "unit"),
    (
        (_s3_output, "S3", "S3-00", "XRPUSDT"),
        (_s4_output, "S4", "S4-00", "XRP-DOGE"),
    ),
)
def test_one_global_invocation_exact_equations_closed_histograms_and_hash(
    evidence, output_factory, strategy, config_id, unit
):
    result = evidence.build_unique_generator_evidence(
        output_factory(), fold_or_full_window="fold-00", phase="selected_oos"
    )
    assert result.strategy == strategy
    assert result.config_id == config_id
    assert result.global_invocation_count == 1
    assert result.evaluated_decision_units == 3
    assert result.no_signal == 1
    assert result.candidate == 2
    assert result.generator_rejected == 1
    assert result.generator_accepted == 1
    assert result.evaluated_decision_units == result.no_signal + result.candidate
    assert result.candidate == result.generator_rejected + result.generator_accepted
    assert (
        sum(value for _, value in result.no_signal_reason_histogram) == result.no_signal
    )
    assert (
        sum(value for _, value in result.generator_rejection_reason_histogram)
        == result.generator_rejected
    )
    assert all(type(value) is int for _, value in result.outcome_histogram)
    assert all(key for key, _ in result.no_signal_reason_histogram)
    assert all(key for key, _ in result.generator_rejection_reason_histogram)
    assert any(value == 0 for _, value in result.no_signal_reason_histogram)
    assert (
        result.content_hash
        == {
            "S3": "895fd170fed051c3ed7f18bf51dd18092c5743e4f95dbc1b59e23e339f18bfc4",
            "S4": "e3b1baca166f911141d97adebf56a5df818dd917973c8679b428160416f57058",
        }[strategy]
    )
    assert result.accepted_identities[0].symbol_or_pair == unit
    assert set(result.accepted_identities).isdisjoint(result.rejected_identities)


def test_container_order_invariance_and_one_ulp_candidate_sensitivity(evidence):
    output = _s3_output()
    baseline = evidence.build_unique_generator_evidence(
        output, fold_or_full_window="fold-00", phase="selected_oos"
    )
    permuted = dataclasses.replace(output, decisions=tuple(reversed(output.decisions)))
    assert (
        evidence.build_unique_generator_evidence(
            permuted, fold_or_full_window="fold-00", phase="selected_oos"
        ).content_hash
        == baseline.content_hash
    )
    changed_candidate = dataclasses.replace(
        output.accepted[0], S=math.nextafter(output.accepted[0].S, math.inf)
    )
    changed_decisions = tuple(
        dataclasses.replace(decision, candidate=changed_candidate)
        if decision.status == "GENERATOR_ACCEPTED"
        else decision
        for decision in output.decisions
    )
    changed = dataclasses.replace(
        output, decisions=changed_decisions, accepted=(changed_candidate,)
    )
    mutated = evidence.build_unique_generator_evidence(
        changed, fold_or_full_window="fold-00", phase="selected_oos"
    )
    assert mutated.accepted_identities == baseline.accepted_identities
    assert mutated.content_hash != baseline.content_hash


def test_collision_and_per_unit_regeneration_fail_closed(evidence):
    output = _s3_output()
    with pytest.raises(ValueError):
        s3.S3GeneratorOutput(
            "S3",
            "S3-00",
            output.decisions,
            output.accepted,
            (
                s3.S3RejectedCandidate(
                    output.accepted[0],
                    "simultaneous_candidate_arbitration_loser",
                ),
            ),
        )
    with pytest.raises(TypeError):
        evidence.build_unique_generator_evidence(
            output.accepted[0],
            fold_or_full_window="fold-00",
            phase="selected_oos",
        )
    with pytest.raises(ValueError):
        evidence.build_unique_generator_evidence(
            dataclasses.replace(output, decisions=output.decisions[:2]),
            fold_or_full_window="fold-00",
            phase="selected_oos",
        )


def test_taxonomies_are_ordered_closed_contract_seals_and_exclude_h4_h2_state(evidence):
    assert evidence.S3_NO_SIGNAL_TAXONOMY == s3.S3_NO_SIGNAL_REASONS
    assert evidence.S4_NO_SIGNAL_TAXONOMY == s4.S4_NO_SIGNAL_REASONS
    assert evidence.S3_GENERATOR_REJECTION_TAXONOMY == s3.S3_GENERATOR_REJECTION_REASONS
    assert evidence.S4_GENERATOR_REJECTION_TAXONOMY == s4.S4_GENERATOR_REJECTION_REASONS
    for strategy, no_signal, rejected in (
        (
            "S3",
            evidence.S3_NO_SIGNAL_TAXONOMY,
            evidence.S3_GENERATOR_REJECTION_TAXONOMY,
        ),
        (
            "S4",
            evidence.S4_NO_SIGNAL_TAXONOMY,
            evidence.S4_GENERATOR_REJECTION_TAXONOMY,
        ),
    ):
        payload = strategy_contract_payload(strategy)
        assert payload["no_signal_reasons"] == list(no_signal)
        assert payload["generator_rejection_reasons"] == list(rejected)
        assert all(
            forbidden not in reason
            for reason in no_signal + rejected
            for forbidden in ("funding", "horizon", "engine_state")
        )
    signature = inspect.signature(evidence.build_unique_generator_evidence)
    assert "scenario" not in signature.parameters
    assert not hasattr(evidence, "rerank_after_funding_rejection")


def test_winner_payload_exposes_h4_boundary_but_computes_no_funding(evidence):
    s3_winner = _s3_output().accepted[0]
    s4_winner = _s4_output().accepted[0]
    assert s3_winner.side in ("long", "short")
    assert s3_winner.entry_deadline_ts == s3_winner.decision_ts + 60_000
    assert s3_winner.max_hold_4h_bars == 12
    assert s4_winner.side in ("short_a_long_b", "long_a_short_b")
    assert s4_winner.weight_a + s4_winner.weight_b == 1.0
    assert s4_winner.entry_deadline_ts == s4_winner.decision_ts + 60_000
    assert s4_winner.max_hold_4h_bars == 9
    assert "funding" not in {field.name for field in dataclasses.fields(s3_winner)}
    assert "funding" not in {field.name for field in dataclasses.fields(s4_winner)}
