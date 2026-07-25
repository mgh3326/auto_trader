from __future__ import annotations

import math
from decimal import Decimal

import pytest

from research.nautilus_scalping.trial_evidence import (
    TrialEvidenceError,
    build_trial_evidence,
    parse_trial_evidence,
)


def _cost() -> dict[str, float]:
    return {
        "fee_bps": 4.0,
        "half_spread_bps": 0.0,
        "slippage_bps": 2.0,
    }


def test_evaluated_trial_evidence_round_trips_one_canonical_schema() -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )

    evidence = parse_trial_evidence(payload)

    assert payload == {
        "schema_version": "honest_trial.v3",
        "producer": "autoresearch",
        "producer_version": "1",
        "parameter_key": "params-sha256",
        "config_hash": "config-sha256",
        "execution_cost": _cost(),
        "sharpe": 1.25,
        "sharpe_method": "mean_cv_fold_sharpe",
        "p_value": 0.01,
        "p_value_method": "one_sided_normal_cv_fold_sharpe",
        "sample_size": 4,
        "validation_score": 2.5,
        "selection_score_method": "canonical_cv_score",
    }
    assert evidence.parameter_key == "params-sha256"
    assert evidence.schema_version == "honest_trial.v3"
    assert evidence.producer == "autoresearch"
    assert evidence.producer_version == "1"
    assert evidence.sharpe == pytest.approx(1.25)
    assert evidence.sharpe_method == "mean_cv_fold_sharpe"
    assert evidence.p_value == pytest.approx(0.01)
    assert evidence.p_value_method == "one_sided_normal_cv_fold_sharpe"
    assert evidence.sample_size == 4
    assert evidence.validation_score == pytest.approx(2.5)
    assert evidence.selection_score_method == "canonical_cv_score"


@pytest.mark.parametrize("field", ["sharpe", "p_value", "validation_score"])
@pytest.mark.parametrize("value", [True, "1.25", Decimal("1.25")])
def test_parser_rejects_non_json_number_statistics(field: str, value) -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )
    payload[field] = value

    with pytest.raises(TrialEvidenceError) as exc_info:
        parse_trial_evidence(payload)

    assert exc_info.value.reason_code == "invalid_trial_evidence"


@pytest.mark.parametrize("cost_key", ["fee_bps", "half_spread_bps", "slippage_bps"])
@pytest.mark.parametrize("value", [True, "1.25", Decimal("1.25")])
def test_parser_rejects_non_json_number_execution_costs(cost_key: str, value) -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )
    payload["execution_cost"][cost_key] = value

    with pytest.raises(TrialEvidenceError) as exc_info:
        parse_trial_evidence(payload)

    assert exc_info.value.reason_code == "invalid_trial_evidence"


def test_parser_accepts_json_integer_and_float_numbers() -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost={"fee_bps": 4, "half_spread_bps": 0.0, "slippage_bps": 2},
        sharpe=1,
        p_value=0.0,
        sample_size=4,
        validation_score=2,
    )

    evidence = parse_trial_evidence(payload)

    assert evidence.sharpe == 1.0
    assert evidence.p_value == 0.0
    assert evidence.validation_score == 2.0


@pytest.mark.parametrize(
    "path",
    [
        ("sharpe",),
        ("p_value",),
        ("validation_score",),
        ("execution_cost", "fee_bps"),
        ("execution_cost", "half_spread_bps"),
        ("execution_cost", "slippage_bps"),
    ],
)
@pytest.mark.parametrize("value", [-0.0, 1e20], ids=["negative-zero", "float-to-int"])
def test_parser_rejects_jsonb_hash_unstable_numbers(
    path: tuple[str, ...], value: float
) -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )
    if len(path) == 1:
        payload[path[0]] = value
    else:
        payload[path[0]][path[1]] = value

    with pytest.raises(TrialEvidenceError) as exc_info:
        parse_trial_evidence(payload)

    assert exc_info.value.reason_code == "non_finite_trial_evidence"


@pytest.mark.parametrize(
    "path",
    [
        ("sharpe",),
        ("p_value",),
        ("validation_score",),
        ("execution_cost", "fee_bps"),
        ("execution_cost", "half_spread_bps"),
        ("execution_cost", "slippage_bps"),
    ],
)
def test_parser_rejects_oversized_json_integers(path: tuple[str, ...]) -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )
    if len(path) == 1:
        payload[path[0]] = 10**10000
    else:
        payload[path[0]][path[1]] = 10**10000

    with pytest.raises(TrialEvidenceError) as exc_info:
        parse_trial_evidence(payload)

    assert exc_info.value.reason_code == "non_finite_trial_evidence"


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"sharpe": math.nan}, "non_finite_trial_evidence"),
        ({"p_value": math.inf}, "non_finite_trial_evidence"),
        ({"p_value": -0.01}, "invalid_trial_p_value"),
        ({"p_value": 1.01}, "invalid_trial_p_value"),
        ({"sample_size": 1}, "insufficient_trial_sample"),
        ({"parameter_key": ""}, "invalid_trial_evidence"),
        ({"config_hash": ""}, "invalid_trial_evidence"),
        ({"validation_score": math.nan}, "non_finite_trial_evidence"),
    ],
)
def test_evaluated_trial_evidence_rejects_invalid_statistics(
    overrides: dict, reason: str
) -> None:
    values = {
        "parameter_key": "params-sha256",
        "config_hash": "config-sha256",
        "execution_cost": _cost(),
        "sharpe": 1.25,
        "p_value": 0.01,
        "sample_size": 4,
        "validation_score": 2.5,
    }
    values.update(overrides)

    with pytest.raises(TrialEvidenceError) as exc_info:
        build_trial_evidence(**values)

    assert exc_info.value.reason_code == reason


def test_evaluated_trial_evidence_rejects_json_unserializable_sample_size() -> None:
    with pytest.raises(TrialEvidenceError) as exc_info:
        build_trial_evidence(
            parameter_key="params-sha256",
            config_hash="config-sha256",
            execution_cost=_cost(),
            sharpe=1.25,
            p_value=0.01,
            sample_size=10**5000,
            validation_score=2.5,
        )

    assert exc_info.value.reason_code == "non_finite_trial_evidence"


def test_parser_accepts_v1_only_as_legacy_evidence_without_selection_authority() -> (
    None
):
    legacy = {
        "schema_version": "honest_trial.v1",
        "parameter_key": "params-sha256",
        "config_hash": "config-sha256",
        "execution_cost": _cost(),
        "sharpe": 1.25,
        "sharpe_method": "mean_cv_fold_sharpe",
        "p_value": 0.01,
        "p_value_method": "one_sided_normal_cv_fold_sharpe",
        "sample_size": 4,
    }

    evidence = parse_trial_evidence(legacy)

    assert evidence.sharpe_method == "mean_cv_fold_sharpe"
    assert evidence.p_value_method == "one_sided_normal_cv_fold_sharpe"
    assert evidence.validation_score is None
    assert evidence.selection_score_method is None


def test_parser_accepts_v2_only_as_legacy_evidence_without_producer_authority() -> None:
    legacy = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )
    legacy["schema_version"] = "honest_trial.v2"
    legacy.pop("producer")
    legacy.pop("producer_version")

    evidence = parse_trial_evidence(legacy)

    assert evidence.schema_version == "honest_trial.v2"
    assert evidence.producer is None
    assert evidence.producer_version is None
    assert evidence.validation_score == pytest.approx(2.5)
    assert evidence.selection_score_method == "canonical_cv_score"


def test_parser_rejects_forged_selection_score_method() -> None:
    payload = build_trial_evidence(
        parameter_key="params-sha256",
        config_hash="config-sha256",
        execution_cost=_cost(),
        sharpe=1.25,
        p_value=0.01,
        sample_size=4,
        validation_score=2.5,
    )
    payload["selection_score_method"] = "caller_favorable"

    with pytest.raises(TrialEvidenceError) as exc_info:
        parse_trial_evidence(payload)

    assert exc_info.value.reason_code == "invalid_trial_evidence"


def test_parser_rejects_unknown_or_forged_schema() -> None:
    with pytest.raises(TrialEvidenceError) as exc_info:
        parse_trial_evidence(
            {
                "schema_version": "honest_trial.v0",
                "parameter_key": "params-sha256",
            }
        )

    assert exc_info.value.reason_code == "invalid_trial_evidence"
