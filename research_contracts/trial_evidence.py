"""Canonical producer/consumer contract for one evaluated research trial."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .jsonb_numbers import jsonb_stable_number

SCHEMA_VERSION = "honest_trial.v3"
SELECTION_SCHEMA_VERSION = "honest_trial.v2"
LEGACY_SCHEMA_VERSION = "honest_trial.v1"
PRODUCER = "autoresearch"
PRODUCER_VERSION = "1"
SHARPE_METHOD = "mean_cv_fold_sharpe"
P_VALUE_METHOD = "one_sided_normal_cv_fold_sharpe"
SELECTION_SCORE_METHOD = "canonical_cv_score"
_EXECUTION_COST_KEYS = frozenset({"fee_bps", "half_spread_bps", "slippage_bps"})


class TrialEvidenceError(ValueError):
    """Fail-closed trial-evidence validation error with a stable reason code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


@dataclass(frozen=True)
class TrialEvidence:
    schema_version: str
    producer: str | None
    producer_version: str | None
    parameter_key: str
    config_hash: str
    execution_cost: dict[str, float]
    sharpe: float
    sharpe_method: str
    p_value: float
    p_value_method: str
    sample_size: int
    validation_score: float | None
    selection_score_method: str | None


def _finite_number(value: Any) -> float:
    if type(value) not in {int, float}:
        raise TrialEvidenceError("invalid_trial_evidence")
    stable = jsonb_stable_number(value)
    if stable is None:
        raise TrialEvidenceError("non_finite_trial_evidence")
    return float(stable)


def build_trial_evidence(
    *,
    parameter_key: str,
    config_hash: str,
    execution_cost: dict[str, float],
    sharpe: float,
    p_value: float,
    sample_size: int,
    validation_score: float,
) -> dict[str, Any]:
    """Build the only accepted JSON payload for an evaluated trial."""
    payload: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "producer": PRODUCER,
        "producer_version": PRODUCER_VERSION,
        "parameter_key": parameter_key,
        "config_hash": config_hash,
        "execution_cost": dict(execution_cost),
        "sharpe": sharpe,
        "sharpe_method": SHARPE_METHOD,
        "p_value": p_value,
        "p_value_method": P_VALUE_METHOD,
        "sample_size": sample_size,
        "validation_score": validation_score,
        "selection_score_method": SELECTION_SCORE_METHOD,
    }
    parse_trial_evidence(payload)
    return payload


def parse_trial_evidence(payload: Any) -> TrialEvidence:
    """Parse exact canonical evidence; unknown or incomplete shapes fail closed."""
    if not isinstance(payload, dict):
        raise TrialEvidenceError("invalid_trial_evidence")
    common_keys = {
        "schema_version",
        "parameter_key",
        "config_hash",
        "execution_cost",
        "sharpe",
        "sharpe_method",
        "p_value",
        "p_value_method",
        "sample_size",
    }
    schema_version = payload.get("schema_version")
    expected_keys = common_keys
    producer: str | None = None
    producer_version: str | None = None
    if schema_version == SCHEMA_VERSION:
        expected_keys = common_keys | {
            "validation_score",
            "selection_score_method",
            "producer",
            "producer_version",
        }
        if payload.get("producer") != PRODUCER:
            raise TrialEvidenceError("invalid_trial_evidence")
        if payload.get("producer_version") != PRODUCER_VERSION:
            raise TrialEvidenceError("invalid_trial_evidence")
        producer = PRODUCER
        producer_version = PRODUCER_VERSION
    elif schema_version == SELECTION_SCHEMA_VERSION:
        expected_keys = common_keys | {
            "validation_score",
            "selection_score_method",
        }
    elif schema_version != LEGACY_SCHEMA_VERSION:
        raise TrialEvidenceError("invalid_trial_evidence")
    if set(payload) != expected_keys:
        raise TrialEvidenceError("invalid_trial_evidence")
    if payload.get("sharpe_method") != SHARPE_METHOD:
        raise TrialEvidenceError("invalid_trial_evidence")
    if payload.get("p_value_method") != P_VALUE_METHOD:
        raise TrialEvidenceError("invalid_trial_evidence")

    parameter_key = payload.get("parameter_key")
    config_hash = payload.get("config_hash")
    if not isinstance(parameter_key, str) or not parameter_key:
        raise TrialEvidenceError("invalid_trial_evidence")
    if not isinstance(config_hash, str) or not config_hash:
        raise TrialEvidenceError("invalid_trial_evidence")

    raw_cost = payload.get("execution_cost")
    if not isinstance(raw_cost, dict) or set(raw_cost) != _EXECUTION_COST_KEYS:
        raise TrialEvidenceError("invalid_trial_evidence")
    execution_cost = {key: _finite_number(raw_cost[key]) for key in raw_cost}

    sharpe = _finite_number(payload.get("sharpe"))
    p_value = _finite_number(payload.get("p_value"))
    if p_value < 0 or p_value > 1:
        raise TrialEvidenceError("invalid_trial_p_value")
    sample_size = payload.get("sample_size")
    if not isinstance(sample_size, int) or isinstance(sample_size, bool):
        raise TrialEvidenceError("invalid_trial_evidence")
    if jsonb_stable_number(sample_size) is None:
        raise TrialEvidenceError("non_finite_trial_evidence")
    if sample_size < 2:
        raise TrialEvidenceError("insufficient_trial_sample")
    validation_score: float | None = None
    selection_score_method: str | None = None
    if schema_version in {SCHEMA_VERSION, SELECTION_SCHEMA_VERSION}:
        if payload.get("selection_score_method") != SELECTION_SCORE_METHOD:
            raise TrialEvidenceError("invalid_trial_evidence")
        validation_score = _finite_number(payload.get("validation_score"))
        selection_score_method = SELECTION_SCORE_METHOD
    return TrialEvidence(
        schema_version=schema_version,
        producer=producer,
        producer_version=producer_version,
        parameter_key=parameter_key,
        config_hash=config_hash,
        execution_cost=execution_cost,
        sharpe=sharpe,
        sharpe_method=SHARPE_METHOD,
        p_value=p_value,
        p_value_method=P_VALUE_METHOD,
        sample_size=sample_size,
        validation_score=validation_score,
        selection_score_method=selection_score_method,
    )
