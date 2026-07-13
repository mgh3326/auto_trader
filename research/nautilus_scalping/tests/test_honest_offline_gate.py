from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import frozen_config as fc
import honest_offline_gate as hog
import pytest


def _candidate(key: str, score: float) -> hog.SelectionCandidate:
    return hog.SelectionCandidate(
        parameter_key=key,
        validation_score=score,
        fold_metrics=({"fold": "validation", "net_return": score},),
    )


def _pit(**overrides) -> hog.PITEvidence:
    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    values = {
        "manifest_hash": "data-hash",
        "manifest_timestamp": cutoff - timedelta(days=2),
        "max_observation_timestamp": cutoff,
        "information_cutoff": cutoff,
    }
    values.update(overrides)
    return hog.PITEvidence(**values)


def test_selection_cannot_read_sealed_oos_and_is_deterministic() -> None:
    candidates = [_candidate("slow", 1.0), _candidate("fast", 2.0)]

    first = hog.select_parameters(candidates)
    changed_oos = hog.SealedOOS(returns=(100.0, -100.0), metrics={"net": -999.0})

    assert first == hog.select_parameters(candidates)
    assert first.selected_parameter == "fast"
    with pytest.raises(TypeError):
        hog.select_parameters(candidates, sealed_oos=changed_oos)


@pytest.mark.parametrize(
    ("evidence", "expected_reason"),
    [
        (_pit(manifest_hash=None), "missing_pit_manifest"),
        (_pit(information_cutoff=None), "missing_information_cutoff"),
        (_pit(manifest_hash="wrong"), "pit_manifest_hash_mismatch"),
        (
            _pit(manifest_timestamp=datetime(2026, 1, 2, tzinfo=UTC)),
            "pit_manifest_after_cutoff",
        ),
        (
            _pit(max_observation_timestamp=datetime(2026, 1, 2, tzinfo=UTC)),
            "pit_observation_after_cutoff",
        ),
    ],
)
def test_pit_evidence_fails_closed(
    evidence: hog.PITEvidence, expected_reason: str
) -> None:
    assert expected_reason in hog.validate_pit_evidence(
        evidence, expected_manifest_hash="data-hash"
    )


def test_pit_evidence_accepts_cutoff_boundary() -> None:
    assert hog.validate_pit_evidence(_pit(), expected_manifest_hash="data-hash") == ()


def test_dsr_returns_probability_for_finite_variable_sample() -> None:
    result = hog.deflated_sharpe_ratio(
        returns=(0.01, 0.03, -0.01, 0.02, 0.04, -0.005, 0.015, 0.025),
        completed_trial_sharpes=(0.2, 0.5, 0.8),
        total_trials=3,
        min_observations=6,
    )

    assert result.reason_codes == ()
    assert result.value is not None
    assert 0.0 <= result.value <= 1.0


@pytest.mark.parametrize(
    ("returns", "trial_sharpes", "total", "reason"),
    [
        ((0.01, 0.02), (0.1, 0.2), 2, "insufficient_dsr_sample"),
        ((0.01,) * 8, (0.1, 0.2), 2, "zero_dsr_variance"),
        (
            (0.01, 0.02, 0.03, float("inf"), 0.01, 0.02),
            (0.1, 0.2),
            2,
            "non_finite_dsr_input",
        ),
        (
            (0.01, 0.02, -0.01, 0.03, 0.02, -0.02),
            (0.1, 0.1),
            2,
            "zero_dsr_variance",
        ),
    ],
)
def test_dsr_edge_cases_fail_closed(returns, trial_sharpes, total, reason) -> None:
    result = hog.deflated_sharpe_ratio(
        returns=returns,
        completed_trial_sharpes=trial_sharpes,
        total_trials=total,
        min_observations=6,
    )

    assert result.value is None
    assert result.reason_codes == (reason,)


def test_pbo_returns_probability_for_valid_cscv_matrix() -> None:
    result = hog.probability_backtest_overfitting(
        candidate_returns={
            "a": (0.010, 0.011, 0.012, 0.013, 0.014, 0.015, 0.016, 0.017),
            "b": (0.020, 0.021, 0.022, 0.023, 0.024, 0.025, 0.026, 0.027),
            "c": (0.030, 0.031, 0.032, 0.033, 0.034, 0.035, 0.036, 0.037),
        },
        slices=4,
    )

    assert result.reason_codes == ()
    assert result.value is not None
    assert 0.0 <= result.value <= 1.0


@pytest.mark.parametrize(
    ("candidate_returns", "slices", "reason"),
    [
        ({"a": (1.0, 2.0, 3.0, 4.0)}, 4, "insufficient_pbo_sample"),
        (
            {"a": (1.0, 2.0, 3.0, 4.0), "b": (2.0, 1.0, 4.0, 3.0)},
            3,
            "invalid_pbo_slices",
        ),
        (
            {
                "a": (1.0, 2.0, float("nan"), 4.0),
                "b": (2.0, 1.0, 4.0, 3.0),
            },
            4,
            "non_finite_pbo_input",
        ),
    ],
)
def test_pbo_edge_cases_fail_closed(candidate_returns, slices, reason) -> None:
    result = hog.probability_backtest_overfitting(candidate_returns, slices=slices)

    assert result.value is None
    assert result.reason_codes == (reason,)


def test_benjamini_hochberg_rejects_expected_hypotheses() -> None:
    result = hog.benjamini_hochberg(
        {"strong": 0.001, "weak": 0.04, "null": 0.8}, alpha=0.05
    )

    assert result.reason_codes == ()
    assert result.rejected == ("strong",)


@pytest.mark.parametrize(
    ("p_values", "reason"),
    [
        ({}, "missing_fdr_evidence"),
        ({"bad": float("nan")}, "non_finite_fdr_input"),
        ({"bad": 1.1}, "invalid_fdr_p_value"),
    ],
)
def test_benjamini_hochberg_invalid_inputs_fail_closed(p_values, reason) -> None:
    result = hog.benjamini_hochberg(p_values, alpha=0.05)

    assert result.rejected == ()
    assert result.reason_codes == (reason,)


def test_gate_artifact_requires_all_baselines_and_hashes_deterministically() -> None:
    config = hog.HonestGateConfig()
    kwargs = {
        "experiment_id": "experiment",
        "run_id": "run",
        "config_hash": config.config_hash(),
        "data_hash": "data-hash",
        "selection": hog.select_parameters([_candidate("fast", 2.0)]),
        "sealed_oos": hog.SealedOOS(
            returns=(0.01, 0.02, -0.005, 0.03, 0.01, 0.02),
            metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
        ),
        "pit_evidence": _pit(),
        "accounting": {
            "total_trials": 3,
            "outcome_counts": {
                "completed": 2,
                "rejected": 1,
                "crashed": 0,
                "timeout": 0,
            },
        },
        "dsr": hog.StatisticResult(0.99),
        "pbo": hog.StatisticResult(0.25),
        "fdr": hog.FDRResult(("fast",)),
        "candidate_p_value_key": "fast",
        "economic_edge_bps": 2.0,
        "fold_metrics": ({"fold": "validation", "net_return": 0.04},),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "cost_stress": {"net_return": 0.04},
        "observed_mdd_pct": 4.0,
        "config": config,
    }

    first = hog.build_gate_artifact(**kwargs)
    second = hog.build_gate_artifact(**kwargs)

    assert first.promotable is True
    assert first.reason_codes == ()
    assert first.artifact_hash == second.artifact_hash
    assert set(first.baselines) == set(hog.REQUIRED_BASELINES)

    missing = dict(kwargs)
    missing["baselines"] = {"cash": 0.0}
    failed = hog.build_gate_artifact(**missing)
    assert failed.promotable is False
    assert failed.reason_codes == ("missing_required_baseline",)


def test_every_honest_gate_definition_changes_config_hash() -> None:
    config = hog.HonestGateConfig()
    changes = [
        {"dsr_probability_threshold": 0.99},
        {"pbo_max": 0.4},
        {"fdr_alpha": 0.01},
        {"economic_triviality_floor_bps": 2.0},
        {"baseline_names": ("cash",)},
        {"taker_bps": 8.0},
        {"half_spread_bps": 2.0},
        {"slippage_bps": 3.0},
        {"mdd_target_pct": 5.0},
    ]

    for change in changes:
        assert (
            dataclasses.replace(config, **change).config_hash() != config.config_hash()
        )


def test_honest_gate_reuses_campaign_config() -> None:
    assert hog.HonestGateConfig is fc.CampaignConfig
