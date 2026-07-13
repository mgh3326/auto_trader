from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import frozen_config as fc
import honest_offline_gate as hog
import pytest

from research_contracts.canonical_hash import canonical_sha256


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


def _valid_artifact_kwargs() -> dict:
    config = hog.HonestGateConfig(
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )
    return {
        "experiment_id": "experiment",
        "run_id": "run",
        "config_hash": config.config_hash(),
        "data_hash": "data-hash",
        "selection": hog.select_parameters([_candidate("fast", 2.0)]),
        "parameter_provenance": {"fast": "params-fast"},
        "sealed_oos": hog.SealedOOS(
            returns=(0.01, 0.02, -0.005, 0.03, 0.01, 0.02),
            metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
        ),
        "pit_evidence": _pit(),
        "accounting": {"total_trials": 1, "outcome_counts": {}},
        "dsr": hog.StatisticResult(0.99),
        "pbo": hog.StatisticResult(0.25),
        "fdr": hog.FDRResult(("fast",)),
        "economic_edge_bps": 2.0,
        "fold_metrics": (),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "execution_cost": {
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        "random_baseline": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
        "cost_stress": {"1.0": 0.08, "1.5": 0.04, "2.0": 0.01},
        "registered_information_cutoff": _pit().information_cutoff,
        "precondition_reasons": (),
        "config": config,
    }


def test_selection_cannot_read_sealed_oos_and_is_deterministic() -> None:
    candidates = [_candidate("slow", 1.0), _candidate("fast", 2.0)]

    first = hog.select_parameters(candidates)
    changed_oos = hog.SealedOOS(returns=(100.0, -100.0), metrics={"net": -999.0})

    assert first == hog.select_parameters(candidates)
    assert first.selected_parameter == "fast"
    with pytest.raises(TypeError):
        hog.select_parameters(candidates, sealed_oos=changed_oos)


@pytest.mark.parametrize("value", [True, "0.01", Decimal("0.01")])
def test_authoritative_statistics_reject_coercible_non_json_numbers(value) -> None:
    with pytest.raises(ValueError):
        hog.select_parameters([_candidate("candidate", value)])

    dsr = hog.deflated_sharpe_ratio(
        returns=(value, 0.03, -0.01, 0.02, 0.04, -0.005),
        completed_trial_sharpes=(0.2, 0.5),
        total_trials=2,
        min_observations=6,
    )
    assert dsr.reason_codes == ("non_finite_dsr_input",)

    pbo = hog.probability_backtest_overfitting(
        {
            "a": (value, 0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07),
            "b": (0.02,) * 8,
        },
        slices=4,
    )
    assert pbo.reason_codes == ("non_finite_pbo_input",)

    fdr = hog.benjamini_hochberg({"candidate": value}, alpha=0.05)
    assert fdr.reason_codes == ("non_finite_fdr_input",)


@pytest.mark.parametrize("value", [True, "0.01", Decimal("0.01")])
@pytest.mark.parametrize("field", ["returns", "metrics"])
def test_sealed_oos_artifact_rejects_coercible_non_json_numbers(
    field: str, value
) -> None:
    sealed = hog.SealedOOS(
        returns=(0.01, 0.02),
        metrics={"net_return": 0.03, "max_drawdown_pct": 4.0},
    )
    if field == "returns":
        sealed = dataclasses.replace(sealed, returns=(value, 0.02))
    else:
        sealed = dataclasses.replace(
            sealed,
            metrics={"net_return": value, "max_drawdown_pct": 4.0},
        )

    with pytest.raises(hog.SealedOOSArtifactError):
        hog.build_sealed_oos_payload(
            experiment_id="experiment",
            config_hash="config",
            data_hash="data",
            window={"start": "2026-01-01", "end": "2026-01-31"},
            sealed_oos=sealed,
        )


@pytest.mark.parametrize("value", [-0.0, 1e20], ids=["negative-zero", "float-to-int"])
@pytest.mark.parametrize("field", ["returns", "metrics"])
def test_sealed_oos_artifact_rejects_jsonb_unstable_numbers(
    field: str, value: float
) -> None:
    sealed = hog.SealedOOS(
        returns=(0.01, 0.02),
        metrics={"net_return": 0.03, "max_drawdown_pct": 4.0},
    )
    if field == "returns":
        sealed = dataclasses.replace(sealed, returns=(value, 0.02))
    else:
        sealed = dataclasses.replace(
            sealed,
            metrics={"net_return": value, "max_drawdown_pct": 4.0},
        )

    with pytest.raises(hog.SealedOOSArtifactError):
        hog.build_sealed_oos_payload(
            experiment_id="experiment",
            config_hash="config",
            data_hash="data",
            window={"start": "2026-01-01", "end": "2026-01-31"},
            sealed_oos=sealed,
        )


def test_sealed_oos_artifact_requires_hash_bound_max_drawdown() -> None:
    with pytest.raises(hog.SealedOOSArtifactError):
        hog.build_sealed_oos_payload(
            experiment_id="experiment",
            config_hash="config",
            data_hash="data",
            window={"start": "2026-01-01", "end": "2026-01-31"},
            sealed_oos=hog.SealedOOS(
                returns=(0.01, 0.02),
                metrics={"net_return": 0.03},
            ),
        )


def test_sealed_oos_artifact_rejects_negative_max_drawdown() -> None:
    with pytest.raises(hog.SealedOOSArtifactError):
        hog.build_sealed_oos_payload(
            experiment_id="experiment",
            config_hash="config",
            data_hash="data",
            window={"start": "2026-01-01", "end": "2026-01-31"},
            sealed_oos=hog.SealedOOS(
                returns=(0.01, 0.02),
                metrics={"net_return": 0.03, "max_drawdown_pct": -99.0},
            ),
        )


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
        (_pit(manifest_hash=object()), "missing_pit_manifest"),
        (_pit(manifest_timestamp="bad"), "invalid_pit_evidence"),
        (_pit(max_observation_timestamp=object()), "invalid_pit_evidence"),
        (_pit(information_cutoff="bad"), "invalid_pit_evidence"),
    ],
)
def test_pit_evidence_fails_closed(
    evidence: hog.PITEvidence, expected_reason: str
) -> None:
    assert expected_reason in hog.validate_pit_evidence(
        evidence,
        expected_manifest_hash="data-hash",
        registered_information_cutoff=_pit().information_cutoff,
    )


def test_pit_evidence_accepts_cutoff_boundary() -> None:
    assert (
        hog.validate_pit_evidence(
            _pit(),
            expected_manifest_hash="data-hash",
            registered_information_cutoff=_pit().information_cutoff,
        )
        == ()
    )


def test_pit_evidence_binds_registered_cutoff_after_utc_normalization() -> None:
    supplied = _pit(
        information_cutoff=datetime.fromisoformat("2026-01-01T09:00:00+09:00")
    )
    assert (
        hog.validate_pit_evidence(
            supplied,
            expected_manifest_hash="data-hash",
            registered_information_cutoff=datetime(2026, 1, 1),
        )
        == ()
    )

    reasons = hog.validate_pit_evidence(
        _pit(information_cutoff=datetime(2026, 1, 2, tzinfo=UTC)),
        expected_manifest_hash="data-hash",
        registered_information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert "information_cutoff_mismatch" in reasons

    missing = hog.validate_pit_evidence(
        _pit(),
        expected_manifest_hash="data-hash",
        registered_information_cutoff=None,
    )
    assert "missing_information_cutoff" in missing


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
            (0.01, 0.02, 0.03, "bad", 0.01, 0.02),
            (0.1, 0.2),
            2,
            "non_finite_dsr_input",
        ),
        (
            (0.01, 0.02, 0.03, None, 0.01, 0.02),
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
        (
            {
                "a": (1.0, 2.0, "bad", 4.0),
                "b": (2.0, 1.0, 4.0, 3.0),
            },
            4,
            "non_finite_pbo_input",
        ),
        (
            {
                "a": (1.0, 2.0, None, 4.0),
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
        ({"bad": "not-a-number"}, "non_finite_fdr_input"),
        ({"bad": None}, "non_finite_fdr_input"),
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
        "parameter_provenance": {"fast": "params-fast"},
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
        "economic_edge_bps": 2.0,
        "fold_metrics": ({"fold": "validation", "net_return": 0.04},),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "execution_cost": {
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        "random_baseline": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
        "cost_stress": {"1.0": 0.08, "1.5": 0.04, "2.0": 0.01},
        "registered_information_cutoff": _pit().information_cutoff,
        "precondition_reasons": (),
        "config": config,
    }

    first = hog.build_gate_artifact(**kwargs)
    second = hog.build_gate_artifact(**kwargs)

    assert first.promotable is True
    assert first.reason_codes == ()
    assert first.artifact_hash == second.artifact_hash
    assert (
        first.artifact_hash
        == "e10825f116e78bc5ee04ddf9dbdd7d95977584f64772e94e5073585d26419a72"
    )
    assert set(first.baselines) == set(hog.REQUIRED_BASELINES)


def test_gate_artifact_hashes_the_exact_json_native_persisted_payload() -> None:
    kwargs = _valid_artifact_kwargs()
    kwargs["sealed_oos_artifact_id"] = 91

    artifact = hog.build_gate_artifact(**kwargs)
    metrics = artifact.to_metrics()
    round_trip = json.loads(json.dumps(metrics, allow_nan=False))

    assert round_trip == metrics
    artifact_hash = round_trip.pop("artifact_hash")
    assert canonical_sha256(round_trip) == artifact_hash
    assert round_trip["sealed_oos_artifact_id"] == 91
    assert isinstance(round_trip["selection"]["ranking"], list)
    assert isinstance(round_trip["reason_codes"], list)
    assert round_trip["pit"]["information_cutoff"].endswith("+00:00")


@pytest.mark.parametrize("value", [-0.0, 1e20], ids=["negative-zero", "float-to-int"])
def test_gate_artifact_fails_closed_before_hashing_jsonb_unstable_numbers(
    value: float,
) -> None:
    kwargs = _valid_artifact_kwargs()
    kwargs["economic_edge_bps"] = value

    artifact = hog.build_gate_artifact(**kwargs)
    persisted = artifact.to_metrics()
    artifact_hash = persisted.pop("artifact_hash")

    assert artifact.promotable is False
    assert artifact.economic_edge_bps is None
    assert "economic_edge_below_minimum" in artifact.reason_codes
    assert canonical_sha256(persisted) == artifact_hash


def test_gate_artifact_rejects_negative_hash_bound_max_drawdown() -> None:
    kwargs = _valid_artifact_kwargs()
    kwargs["sealed_oos"] = dataclasses.replace(
        kwargs["sealed_oos"],
        metrics={"net_return": 0.08, "max_drawdown_pct": -99.0},
    )

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert artifact.mdd == {"target_pct": 20.0, "observed_pct": -99.0}
    assert "mdd_target_exceeded" in artifact.reason_codes


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda values: values.update(economic_edge_bps="2.0"),
            "economic_edge_below_minimum",
        ),
        (
            lambda values: values.update(
                sealed_oos=dataclasses.replace(
                    values["sealed_oos"],
                    metrics={
                        **values["sealed_oos"].metrics,
                        "max_drawdown_pct": Decimal("4.0"),
                    },
                )
            ),
            "mdd_target_exceeded",
        ),
        (
            lambda values: values["sealed_oos"].metrics.update(net_return="0.08"),
            "invalid_evidence_mapping",
        ),
        (
            lambda values: values["baselines"].update(cash="0.0"),
            "invalid_evidence_mapping",
        ),
        (
            lambda values: values["execution_cost"].update(fee_bps=Decimal("4.0")),
            "invalid_evidence_mapping",
        ),
        (
            lambda values: values["cost_stress"].update({"1.5": "0.04"}),
            "invalid_evidence_mapping",
        ),
        (
            lambda values: values.update(
                fold_metrics=({"fold": "validation", "net_return": "0.04"},)
            ),
            "invalid_fold_metrics",
        ),
        (
            lambda values: values.update(
                selection=dataclasses.replace(
                    values["selection"], validation_scores={"fast": "2.0"}
                )
            ),
            "invalid_selection_evidence",
        ),
        (
            lambda values: values.update(
                accounting={"total_trials": "1", "outcome_counts": {}}
            ),
            "invalid_evidence_mapping",
        ),
        (
            lambda values: values.update(dsr=hog.StatisticResult("0.99")),
            "invalid_evidence_mapping",
        ),
        (
            lambda values: values.update(pbo=hog.StatisticResult(Decimal("0.25"))),
            "invalid_evidence_mapping",
        ),
    ],
)
def test_gate_artifact_rejects_coercible_non_json_numeric_inputs(
    mutate, reason: str
) -> None:
    kwargs = _valid_artifact_kwargs()
    kwargs["sealed_oos"] = dataclasses.replace(
        kwargs["sealed_oos"], metrics=dict(kwargs["sealed_oos"].metrics)
    )
    mutate(kwargs)

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert reason in artifact.reason_codes


def test_gate_artifact_rejects_missing_extra_or_malformed_baselines() -> None:
    kwargs = _valid_artifact_kwargs()
    missing = dict(kwargs)
    missing["baselines"] = {"cash": 0.0}
    failed = hog.build_gate_artifact(**missing)
    assert failed.promotable is False
    assert failed.reason_codes == ("missing_required_baseline",)

    extra = dict(kwargs)
    extra["baselines"] = {**kwargs["baselines"], "caller_favorable": -999.0}
    extra_artifact = hog.build_gate_artifact(**extra)
    assert extra_artifact.promotable is False
    assert "baseline_provenance_mismatch" in extra_artifact.reason_codes

    malformed = dict(kwargs)
    malformed["baselines"] = {**kwargs["baselines"], "cash": "not-a-number"}
    malformed_artifact = hog.build_gate_artifact(**malformed)
    assert malformed_artifact.promotable is False
    assert "baseline_not_beaten" in malformed_artifact.reason_codes
    assert malformed_artifact.artifact_hash


def test_gate_artifact_binds_fdr_to_selected_parameter() -> None:
    config = hog.HonestGateConfig(
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )
    kwargs = {
        "experiment_id": "experiment",
        "run_id": "run",
        "config_hash": config.config_hash(),
        "data_hash": "data-hash",
        "selection": hog.select_parameters([_candidate("fast", 2.0)]),
        "parameter_provenance": {"fast": "params-fast"},
        "sealed_oos": hog.SealedOOS(
            returns=(0.01, 0.02, -0.005, 0.03, 0.01, 0.02),
            metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
        ),
        "pit_evidence": _pit(),
        "accounting": {"total_trials": 1, "outcome_counts": {}},
        "dsr": hog.StatisticResult(0.99),
        "pbo": hog.StatisticResult(0.25),
        "fdr": hog.FDRResult(("unrelated",)),
        "economic_edge_bps": 2.0,
        "fold_metrics": (),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "execution_cost": {
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        "random_baseline": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
        "cost_stress": {"1.0": 0.08, "1.5": 0.04, "2.0": 0.01},
        "registered_information_cutoff": _pit().information_cutoff,
        "precondition_reasons": (),
        "config": config,
    }

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert "fdr_not_significant" in artifact.reason_codes


@pytest.mark.parametrize(
    ("field", "value", "reason"),
    [
        (
            "execution_cost",
            {"fee_bps": 5.0, "half_spread_bps": 0.0, "slippage_bps": 2.0},
            "execution_cost_mismatch",
        ),
        (
            "random_baseline",
            {"seed": 999, "repetitions": 100},
            "random_baseline_provenance_mismatch",
        ),
        (
            "cost_stress",
            {"1.0": 0.08, "2.0": 0.01},
            "cost_stress_provenance_mismatch",
        ),
        (
            "cost_stress",
            {"1.0": 0.08, "1.5": float("nan"), "2.0": 0.01},
            "cost_stress_failed",
        ),
        (
            "cost_stress",
            {"1.0": 0.08, "1.5": None, "2.0": 0.01},
            "cost_stress_failed",
        ),
        (
            "execution_cost",
            {"fee_bps": None, "half_spread_bps": 0.0, "slippage_bps": 2.0},
            "execution_cost_mismatch",
        ),
        (
            "baselines",
            {
                "cash": float("inf"),
                "btc_eth_equal_weight": 0.03,
                "same_turnover_random": 0.02,
            },
            "baseline_not_beaten",
        ),
        (
            "baselines",
            {
                "cash": 0.0,
                "btc_eth_equal_weight": 0.03,
                "same_turnover_random": 0.02,
                1: 0.01,
            },
            "invalid_evidence_mapping",
        ),
        (
            "execution_cost",
            {"fee_bps": object(), "half_spread_bps": 0.0, "slippage_bps": 2.0},
            "invalid_evidence_mapping",
        ),
        (
            "random_baseline",
            {1: 847, "repetitions": 100},
            "invalid_evidence_mapping",
        ),
        (
            "cost_stress",
            {1.0: 0.08, "1.5": 0.04, "2.0": 0.01},
            "invalid_evidence_mapping",
        ),
        ("economic_edge_bps", None, "economic_edge_below_minimum"),
        ("economic_edge_bps", "bad", "economic_edge_below_minimum"),
        ("economic_edge_bps", float("nan"), "economic_edge_below_minimum"),
        ("economic_edge_bps", float("inf"), "economic_edge_below_minimum"),
        pytest.param(
            "economic_edge_bps",
            10**10000,
            "economic_edge_below_minimum",
            id="oversized-economic-edge",
        ),
        ("sealed_mdd", None, "mdd_target_exceeded"),
        ("sealed_mdd", "bad", "mdd_target_exceeded"),
        ("sealed_mdd", float("nan"), "mdd_target_exceeded"),
        ("sealed_mdd", float("inf"), "mdd_target_exceeded"),
        pytest.param(
            "sealed_mdd",
            10**10000,
            "mdd_target_exceeded",
            id="oversized-mdd",
        ),
    ],
)
def test_gate_artifact_binds_frozen_execution_provenance(
    field: str, value, reason: str
) -> None:
    config = hog.HonestGateConfig(
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )
    kwargs = {
        "experiment_id": "experiment",
        "run_id": "run",
        "config_hash": config.config_hash(),
        "data_hash": "data-hash",
        "selection": hog.select_parameters([_candidate("fast", 2.0)]),
        "parameter_provenance": {"fast": "params-fast"},
        "sealed_oos": hog.SealedOOS(
            returns=(0.01, 0.02, -0.005, 0.03, 0.01, 0.02),
            metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
        ),
        "pit_evidence": _pit(),
        "accounting": {"total_trials": 1, "outcome_counts": {}},
        "dsr": hog.StatisticResult(0.99),
        "pbo": hog.StatisticResult(0.25),
        "fdr": hog.FDRResult(("fast",)),
        "economic_edge_bps": 2.0,
        "fold_metrics": (),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "execution_cost": {
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        "random_baseline": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
        "cost_stress": {"1.0": 0.08, "1.5": 0.04, "2.0": 0.01},
        "registered_information_cutoff": _pit().information_cutoff,
        "precondition_reasons": (),
        "config": config,
    }
    if field == "sealed_mdd":
        kwargs["sealed_oos"] = dataclasses.replace(
            kwargs["sealed_oos"],
            metrics={
                **kwargs["sealed_oos"].metrics,
                "max_drawdown_pct": value,
            },
        )
    else:
        kwargs[field] = value

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert reason in artifact.reason_codes


@pytest.mark.parametrize(
    "fold_metrics",
    [
        ({"fold": "validation", "net_return": float("nan")},),
        ({1: 0.04},),
        ({"fold": "validation", "nested": {"value": object()}},),
        (object(),),
    ],
)
def test_gate_artifact_seals_malformed_fold_metrics_without_mutation(
    fold_metrics,
) -> None:
    kwargs = _valid_artifact_kwargs()
    kwargs["fold_metrics"] = fold_metrics
    original_first = fold_metrics[0]

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert "invalid_fold_metrics" in artifact.reason_codes
    assert artifact.artifact_hash
    assert fold_metrics[0] is original_first


def test_gate_artifact_seals_malformed_oos_mapping_as_canonical_safe_evidence() -> None:
    kwargs = _valid_artifact_kwargs()
    metrics = {"net_return": 0.08, 1: {"unsupported": object()}}
    kwargs["sealed_oos"] = dataclasses.replace(kwargs["sealed_oos"], metrics=metrics)

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert "invalid_evidence_mapping" in artifact.reason_codes
    assert artifact.oos_metrics == {"net_return": 0.08}
    assert 1 in metrics
    assert artifact.artifact_hash


def test_gate_artifact_seals_malformed_pit_payload_as_canonical_safe_evidence() -> None:
    kwargs = _valid_artifact_kwargs()
    evidence = dataclasses.replace(
        kwargs["pit_evidence"],
        manifest_timestamp=object(),
        max_observation_timestamp="bad",
    )
    kwargs["pit_evidence"] = evidence

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert "invalid_pit_evidence" in artifact.reason_codes
    assert artifact.pit["manifest_timestamp"] is None
    assert artifact.pit["max_observation_timestamp"] is None
    assert evidence.manifest_timestamp is not None
    assert artifact.artifact_hash


def test_gate_artifact_seals_external_precondition_failures() -> None:
    config = hog.HonestGateConfig(
        dsr_probability_threshold=0.0,
        pbo_max=1.0,
    )
    kwargs = {
        "experiment_id": "experiment",
        "run_id": "run",
        "config_hash": config.config_hash(),
        "data_hash": "data-hash",
        "selection": hog.select_parameters([_candidate("fast", 2.0)]),
        "parameter_provenance": {"fast": "params-fast"},
        "sealed_oos": hog.SealedOOS(
            returns=(0.01, 0.02, -0.005, 0.03, 0.01, 0.02),
            metrics={"net_return": 0.08, "max_drawdown_pct": 4.0},
        ),
        "pit_evidence": _pit(),
        "accounting": {"total_trials": 1, "outcome_counts": {}},
        "dsr": hog.StatisticResult(None, ("invalid_trial_evidence",)),
        "pbo": hog.StatisticResult(None, ("invalid_trial_evidence",)),
        "fdr": hog.FDRResult((), ("invalid_trial_evidence",)),
        "economic_edge_bps": 2.0,
        "fold_metrics": (),
        "baselines": {
            "cash": 0.0,
            "btc_eth_equal_weight": 0.03,
            "same_turnover_random": 0.02,
        },
        "execution_cost": {
            "fee_bps": config.taker_bps,
            "half_spread_bps": config.half_spread_bps,
            "slippage_bps": config.slippage_bps,
        },
        "random_baseline": {
            "seed": config.random_baseline_seed,
            "repetitions": config.random_baseline_repetitions,
        },
        "cost_stress": {"1.0": 0.08, "1.5": 0.04, "2.0": 0.01},
        "registered_information_cutoff": _pit().information_cutoff,
        "precondition_reasons": ("incomplete_trial_accounting",),
        "config": config,
    }

    artifact = hog.build_gate_artifact(**kwargs)

    assert artifact.promotable is False
    assert "incomplete_trial_accounting" in artifact.reason_codes


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
