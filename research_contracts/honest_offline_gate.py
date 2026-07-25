"""ROB-847 causal/PIT/trial-aware offline promotion evidence.

The module is deliberately pure. Parameter selection accepts validation-only
objects; sealed OOS has a distinct type consumed only while building the final
artifact. DSR follows Bailey and López de Prado's probabilistic Sharpe
adjustment, PBO uses combinatorially symmetric cross-validation (CSCV), and FDR
uses Benjamini-Hochberg. Invalid or undersized statistical inputs fail closed
with stable reason codes.
"""

from __future__ import annotations

import itertools
import math
import statistics
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from statistics import NormalDist
from typing import Any

from .canonical_hash import canonical_sha256
from .frozen_config import CampaignConfig
from .jsonb_numbers import jsonb_stable_number

__all__ = [
    "FDRResult",
    "GateArtifact",
    "HonestGateConfig",
    "PITEvidence",
    "REQUIRED_BASELINES",
    "SEALED_OOS_ARTIFACT_PATH",
    "SEALED_OOS_PRODUCER",
    "SEALED_OOS_PRODUCER_VERSION",
    "SEALED_OOS_RUNNER",
    "SEALED_OOS_SCHEMA_VERSION",
    "SEALED_OOS_TIMEFRAME",
    "SealedOOS",
    "SealedOOSArtifactError",
    "SelectionCandidate",
    "SelectionResult",
    "StatisticResult",
    "benjamini_hochberg",
    "build_sealed_oos_payload",
    "build_gate_artifact",
    "deflated_sharpe_ratio",
    "probability_backtest_overfitting",
    "parse_sealed_oos_payload",
    "select_parameters",
    "validate_pit_evidence",
]

REQUIRED_BASELINES = ("cash", "btc_eth_equal_weight", "same_turnover_random")
SEALED_OOS_SCHEMA_VERSION = "sealed_oos.v1"
SEALED_OOS_PRODUCER = "honest-offline-gate"
SEALED_OOS_PRODUCER_VERSION = "1"
SEALED_OOS_RUNNER = "sealed-oos-v1"
SEALED_OOS_TIMEFRAME = "sealed-oos"
SEALED_OOS_ARTIFACT_PATH = "sealed-oos://honest-offline-gate/v1"
_EULER_GAMMA = 0.5772156649015329


@dataclass(frozen=True)
class StatisticResult:
    value: float | None
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class FDRResult:
    rejected: tuple[str, ...]
    reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class PITEvidence:
    manifest_hash: str | None
    manifest_timestamp: datetime | None
    max_observation_timestamp: datetime | None
    information_cutoff: datetime | None


@dataclass(frozen=True)
class SelectionCandidate:
    parameter_key: str
    validation_score: float
    fold_metrics: tuple[Mapping[str, Any], ...] = ()


@dataclass(frozen=True)
class SelectionResult:
    selected_parameter: str
    ranking: tuple[str, ...]
    validation_scores: Mapping[str, float]


@dataclass(frozen=True)
class SealedOOS:
    returns: tuple[int | float, ...]
    metrics: Mapping[str, int | float]


HonestGateConfig = CampaignConfig


class SealedOOSArtifactError(ValueError):
    """The append-only sealed-OOS payload is malformed or mismatched."""


@dataclass(frozen=True)
class GateArtifact:
    """One exact JSON-native payload plus the SHA-256 over those exact bytes.

    Compatibility properties deliberately read from the sealed payload.  There
    is no second dataclass/asdict representation which could drift from what is
    hashed and persisted to PostgreSQL JSONB.
    """

    _payload: Mapping[str, Any] = field(repr=False)
    artifact_hash: str

    def __getattr__(self, name: str) -> Any:
        payload = object.__getattribute__(self, "_payload")
        if name in payload:
            if name == "reason_codes":
                return tuple(payload[name])
            return payload[name]
        raise AttributeError(name)

    @property
    def primary_reason(self) -> str:
        return self.reason_codes[0] if self.reason_codes else "ok"

    def to_metrics(self) -> dict[str, Any]:
        metrics = deepcopy(dict(self._payload))
        metrics["artifact_hash"] = self.artifact_hash
        return metrics


def _reasons(*codes: str) -> tuple[str, ...]:
    return tuple(sorted({code for code in codes if code}))


def _json_number(value: Any) -> int | float | None:
    """Return an exact native number that is stable through JSONB."""
    return jsonb_stable_number(value)


def _finite_float(value: Any) -> float | None:
    number = _json_number(value)
    if number is None:
        return None
    try:
        return float(number)
    except OverflowError:
        return None


def _is_finite(values: Sequence[Any]) -> bool:
    return all(_finite_float(value) is not None for value in values)


def _finite_values(values: Sequence[Any]) -> tuple[float, ...] | None:
    parsed = tuple(_finite_float(value) for value in values)
    if any(value is None for value in parsed):
        return None
    return tuple(value for value in parsed if value is not None)


def _finite_numbers(values: Sequence[Any]) -> tuple[int | float, ...] | None:
    parsed = tuple(_json_number(value) for value in values)
    if any(value is None for value in parsed):
        return None
    return tuple(value for value in parsed if value is not None)


def _registered_cutoff_utc(value: Any) -> datetime | None:
    """Normalize persisted timestamptz values; tolerate legacy naive UTC fixtures."""
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _safe_numeric_mapping(
    values: Mapping[Any, Any],
) -> tuple[dict[str, int | float | None], bool]:
    """Return a JSON-safe numeric mapping plus whether the source was valid."""
    safe: dict[str, int | float | None] = {}
    valid = True
    for key, raw in values.items():
        if not isinstance(key, str):
            valid = False
            continue
        value = _json_number(raw)
        if value is None:
            valid = False
        safe[key] = value
    return safe, valid


def _safe_evidence_value(value: Any) -> tuple[Any, bool]:
    """Recursively normalize caller evidence without mutating the source."""
    if value is None or type(value) in {str, bool}:
        return value, True
    if type(value) in {int, float}:
        number = _json_number(value)
        return (number, True) if number is not None else (None, False)
    if isinstance(value, Mapping):
        safe: dict[str, Any] = {}
        valid = True
        for key, item in value.items():
            if not isinstance(key, str):
                valid = False
                continue
            safe_item, item_valid = _safe_evidence_value(item)
            safe[key] = safe_item
            valid = valid and item_valid
        return safe, valid
    if isinstance(value, tuple):
        safe_items = tuple(_safe_evidence_value(item) for item in value)
        return [item for item, _ in safe_items], all(valid for _, valid in safe_items)
    if isinstance(value, list):
        safe_items = [_safe_evidence_value(item) for item in value]
        return [item for item, _ in safe_items], all(valid for _, valid in safe_items)
    return None, False


def _safe_fold_metrics(values: Any) -> tuple[list[dict[str, Any]], bool]:
    if not isinstance(values, Sequence) or isinstance(values, str | bytes):
        return [], False
    safe_metrics: list[dict[str, Any]] = []
    valid = True
    for metrics in values:
        if not isinstance(metrics, Mapping):
            safe_metrics.append({})
            valid = False
            continue
        safe, item_valid = _safe_evidence_value(metrics)
        safe_metrics.append(safe)
        valid = valid and item_valid
        for key, raw in metrics.items():
            if isinstance(raw, str) and key not in {"fold", "name", "split", "window"}:
                valid = False
                safe[key] = None
    return safe_metrics, valid


def _utc_iso(value: Any, *, tolerate_naive_utc: bool = False) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        if not tolerate_naive_utc:
            return None
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _safe_reason_codes(value: Any) -> tuple[list[str], bool]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        return [], False
    result = list(value)
    return result, all(isinstance(item, str) and item for item in result)


def _safe_statistic(result: Any) -> tuple[dict[str, Any], bool]:
    value = getattr(result, "value", None)
    safe_value = None if value is None else _json_number(value)
    reasons, reasons_valid = _safe_reason_codes(getattr(result, "reason_codes", None))
    valid = reasons_valid and (value is None or safe_value is not None)
    return {"value": safe_value, "reason_codes": reasons}, valid


def _safe_fdr(result: Any) -> tuple[dict[str, Any], bool]:
    rejected, rejected_valid = _safe_reason_codes(getattr(result, "rejected", None))
    reasons, reasons_valid = _safe_reason_codes(getattr(result, "reason_codes", None))
    return {
        "rejected": rejected,
        "reason_codes": reasons,
    }, rejected_valid and reasons_valid


def _safe_selection(
    selection: Any,
    parameter_provenance: Mapping[Any, Any],
) -> tuple[dict[str, Any], bool]:
    selected = getattr(selection, "selected_parameter", None)
    raw_ranking = getattr(selection, "ranking", None)
    ranking = (
        list(raw_ranking)
        if isinstance(raw_ranking, Sequence)
        and not isinstance(raw_ranking, str | bytes)
        else []
    )
    scores_raw = getattr(selection, "validation_scores", None)
    scores: dict[str, int | float | None] = {}
    valid = (
        isinstance(selected, str)
        and bool(selected)
        and bool(ranking)
        and all(isinstance(item, str) and item for item in ranking)
        and isinstance(scores_raw, Mapping)
    )
    if isinstance(scores_raw, Mapping):
        scores, scores_valid = _safe_numeric_mapping(scores_raw)
        valid = valid and scores_valid
    provenance: dict[str, str] = {}
    if isinstance(parameter_provenance, Mapping):
        for key, value in parameter_provenance.items():
            if not isinstance(key, str) or not isinstance(value, str) or not value:
                valid = False
                continue
            provenance[key] = value
    else:
        valid = False
    return {
        "selected_parameter": selected if isinstance(selected, str) else "",
        "ranking": ranking,
        "validation_scores": scores,
        "parameter_provenance": provenance,
    }, valid


def _safe_accounting(value: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(value, Mapping):
        return {"total_trials": None, "outcome_counts": {}}, False
    total = _json_number(value.get("total_trials"))
    outcome_raw = value.get("outcome_counts")
    outcome: dict[str, int | float | None] = {}
    valid = type(total) is int and total >= 0 and isinstance(outcome_raw, Mapping)
    if isinstance(outcome_raw, Mapping):
        outcome, outcome_valid = _safe_numeric_mapping(outcome_raw)
        valid = (
            valid
            and outcome_valid
            and all(type(item) is int and item >= 0 for item in outcome.values())
        )
    if set(value) != {"total_trials", "outcome_counts"}:
        valid = False
    return {"total_trials": total, "outcome_counts": outcome}, valid


def build_sealed_oos_payload(
    *,
    experiment_id: str,
    config_hash: str,
    data_hash: str,
    window: Mapping[str, Any],
    sealed_oos: SealedOOS,
) -> dict[str, Any]:
    """Build the exact JSONB payload accepted by the finalizer.

    This function validates but never coerces authoritative numeric evidence.
    The dedicated service writer is responsible for binding it to an immutable
    experiment row and persisting the resulting canonical hash.
    """
    if not all(
        isinstance(value, str) and value
        for value in (experiment_id, config_hash, data_hash)
    ):
        raise SealedOOSArtifactError("invalid_sealed_oos_artifact")
    safe_window, window_valid = _safe_evidence_value(window)
    returns = _finite_numbers(sealed_oos.returns)
    safe_metrics, metrics_valid = _safe_numeric_mapping(sealed_oos.metrics)
    if (
        not window_valid
        or not isinstance(safe_window, dict)
        or returns is None
        or not returns
        or not metrics_valid
        or not {"net_return", "max_drawdown_pct"}.issubset(safe_metrics)
        or safe_metrics["max_drawdown_pct"] < 0
    ):
        raise SealedOOSArtifactError("invalid_sealed_oos_artifact")
    return {
        "schema_version": SEALED_OOS_SCHEMA_VERSION,
        "producer": SEALED_OOS_PRODUCER,
        "producer_version": SEALED_OOS_PRODUCER_VERSION,
        "experiment_id": experiment_id,
        "config_hash": config_hash,
        "data_hash": data_hash,
        "window": safe_window,
        "returns": list(returns),
        "metrics": safe_metrics,
    }


def parse_sealed_oos_payload(
    payload: Any,
    *,
    experiment_id: str,
    config_hash: str,
    data_hash: str,
    window: Mapping[str, Any],
) -> SealedOOS:
    """Verify an exact persisted payload and reconstruct sealed evidence."""
    if not isinstance(payload, dict) or set(payload) != {
        "schema_version",
        "producer",
        "producer_version",
        "experiment_id",
        "config_hash",
        "data_hash",
        "window",
        "returns",
        "metrics",
    }:
        raise SealedOOSArtifactError("invalid_sealed_oos_artifact")
    expected_identity = {
        "schema_version": SEALED_OOS_SCHEMA_VERSION,
        "producer": SEALED_OOS_PRODUCER,
        "producer_version": SEALED_OOS_PRODUCER_VERSION,
        "experiment_id": experiment_id,
        "config_hash": config_hash,
        "data_hash": data_hash,
    }
    if any(payload.get(key) != value for key, value in expected_identity.items()):
        raise SealedOOSArtifactError("sealed_oos_artifact_identity_mismatch")
    safe_window, window_valid = _safe_evidence_value(window)
    if not window_valid or payload.get("window") != safe_window:
        raise SealedOOSArtifactError("sealed_oos_artifact_identity_mismatch")
    raw_returns = payload.get("returns")
    if not isinstance(raw_returns, list):
        raise SealedOOSArtifactError("invalid_sealed_oos_artifact")
    returns = _finite_numbers(raw_returns)
    raw_metrics = payload.get("metrics")
    if not isinstance(raw_metrics, Mapping):
        raise SealedOOSArtifactError("invalid_sealed_oos_artifact")
    metrics, metrics_valid = _safe_numeric_mapping(raw_metrics)
    if (
        returns is None
        or not returns
        or not metrics_valid
        or not {"net_return", "max_drawdown_pct"}.issubset(metrics)
        or metrics["max_drawdown_pct"] < 0
        or metrics != raw_metrics
    ):
        raise SealedOOSArtifactError("invalid_sealed_oos_artifact")
    return SealedOOS(returns=returns, metrics=metrics)


def select_parameters(candidates: Sequence[SelectionCandidate]) -> SelectionResult:
    """Rank by validation evidence only; sealed OOS is absent by construction."""
    if not candidates:
        raise ValueError("selection requires at least one candidate")
    keys = [candidate.parameter_key for candidate in candidates]
    if any(not isinstance(key, str) or not key for key in keys) or len(
        set(keys)
    ) != len(keys):
        raise ValueError("selection candidate keys must be unique and non-empty")
    if not _is_finite([candidate.validation_score for candidate in candidates]):
        raise ValueError("selection validation scores must be finite")
    ordered = sorted(
        candidates,
        key=lambda candidate: (-candidate.validation_score, candidate.parameter_key),
    )
    return SelectionResult(
        selected_parameter=ordered[0].parameter_key,
        ranking=tuple(candidate.parameter_key for candidate in ordered),
        validation_scores={
            candidate.parameter_key: candidate.validation_score for candidate in ordered
        },
    )


def validate_pit_evidence(
    evidence: PITEvidence,
    *,
    expected_manifest_hash: str,
    registered_information_cutoff: datetime | None,
) -> tuple[str, ...]:
    reasons: list[str] = []
    valid_manifest_hash = isinstance(evidence.manifest_hash, str) and bool(
        evidence.manifest_hash
    )
    if not valid_manifest_hash or evidence.manifest_timestamp is None:
        reasons.append("missing_pit_manifest")
    if evidence.information_cutoff is None:
        reasons.append("missing_information_cutoff")
    if registered_information_cutoff is None:
        reasons.append("missing_information_cutoff")
    if valid_manifest_hash and evidence.manifest_hash != expected_manifest_hash:
        reasons.append("pit_manifest_hash_mismatch")

    evidence_timestamps = (
        evidence.manifest_timestamp,
        evidence.max_observation_timestamp,
        evidence.information_cutoff,
    )
    if any(
        value is not None and not isinstance(value, datetime)
        for value in evidence_timestamps
    ):
        reasons.append("invalid_pit_evidence")
        return _reasons(*reasons)
    if any(value is not None and value.tzinfo is None for value in evidence_timestamps):
        reasons.append("invalid_information_cutoff")
        return _reasons(*reasons)
    cutoff = evidence.information_cutoff
    registered_utc = _registered_cutoff_utc(registered_information_cutoff)
    if cutoff is not None and registered_utc is not None:
        if cutoff.astimezone(UTC) != registered_utc:
            reasons.append("information_cutoff_mismatch")
    if cutoff is not None:
        if (
            evidence.manifest_timestamp is not None
            and evidence.manifest_timestamp > cutoff
        ):
            reasons.append("pit_manifest_after_cutoff")
        if (
            evidence.max_observation_timestamp is None
            or evidence.max_observation_timestamp > cutoff
        ):
            reasons.append(
                "missing_pit_manifest"
                if evidence.max_observation_timestamp is None
                else "pit_observation_after_cutoff"
            )
    return _reasons(*reasons)


def deflated_sharpe_ratio(
    returns: Sequence[Any],
    completed_trial_sharpes: Sequence[Any],
    *,
    total_trials: int,
    min_observations: int,
) -> StatisticResult:
    """Calculate DSR using PSR against the expected maximum trial Sharpe.

    `total_trials` is supplied by the registry-backed service, not promotion
    request data. Skewness and non-excess kurtosis correct the PSR denominator.
    """
    values = _finite_values(returns)
    trial_sharpes = _finite_values(completed_trial_sharpes)
    if values is None or trial_sharpes is None:
        return StatisticResult(None, ("non_finite_dsr_input",))
    if (
        type(total_trials) is not int
        or type(min_observations) is not int
        or min_observations < 2
        or len(values) < min_observations
        or total_trials < 1
    ):
        return StatisticResult(None, ("insufficient_dsr_sample",))
    return_std = statistics.stdev(values)
    if return_std == 0:
        return StatisticResult(None, ("zero_dsr_variance",))

    sharpe = statistics.mean(values) / return_std
    centered = [value - statistics.mean(values) for value in values]
    moment_2 = sum(value**2 for value in centered) / len(centered)
    skewness = (sum(value**3 for value in centered) / len(centered)) / (moment_2**1.5)
    kurtosis = (sum(value**4 for value in centered) / len(centered)) / (moment_2**2)

    benchmark_sharpe = 0.0
    if total_trials > 1:
        if len(trial_sharpes) < 2:
            return StatisticResult(None, ("insufficient_dsr_sample",))
        trial_sigma = statistics.stdev(trial_sharpes)
        if trial_sigma == 0:
            return StatisticResult(None, ("zero_dsr_variance",))
        normal = NormalDist()
        benchmark_sharpe = trial_sigma * (
            (1 - _EULER_GAMMA) * normal.inv_cdf(1 - 1 / total_trials)
            + _EULER_GAMMA * normal.inv_cdf(1 - 1 / (total_trials * math.e))
        )

    denominator_squared = 1 - skewness * sharpe + ((kurtosis - 1) / 4) * sharpe**2
    if denominator_squared <= 0 or not math.isfinite(denominator_squared):
        return StatisticResult(None, ("non_finite_dsr_input",))
    z_score = (
        (sharpe - benchmark_sharpe)
        * math.sqrt(len(values) - 1)
        / math.sqrt(denominator_squared)
    )
    probability = NormalDist().cdf(z_score)
    if not math.isfinite(probability):
        return StatisticResult(None, ("non_finite_dsr_input",))
    return StatisticResult(probability)


def probability_backtest_overfitting(
    candidate_returns: Mapping[str, Sequence[Any]], *, slices: int
) -> StatisticResult:
    """Calculate CSCV PBO from IS winners' relative OOS ranks."""
    if len(candidate_returns) < 2:
        return StatisticResult(None, ("insufficient_pbo_sample",))
    if any(not isinstance(key, str) or not key for key in candidate_returns):
        return StatisticResult(None, ("non_finite_pbo_input",))
    if type(slices) is not int or slices < 4 or slices % 2:
        return StatisticResult(None, ("invalid_pbo_slices",))
    rows: dict[str, tuple[float, ...]] = {}
    for key, values in candidate_returns.items():
        parsed = _finite_values(values)
        if parsed is None:
            return StatisticResult(None, ("non_finite_pbo_input",))
        rows[key] = parsed
    lengths = {len(values) for values in rows.values()}
    if len(lengths) != 1 or next(iter(lengths), 0) < slices:
        return StatisticResult(None, ("insufficient_pbo_sample",))
    if any(not _is_finite(values) for values in rows.values()):
        return StatisticResult(None, ("non_finite_pbo_input",))

    count = next(iter(lengths))
    sliced: dict[str, tuple[tuple[float, ...], ...]] = {}
    for key, values in rows.items():
        sliced[key] = tuple(
            values[index * count // slices : (index + 1) * count // slices]
            for index in range(slices)
        )
    overfit = 0
    combinations = 0
    all_slice_indices = set(range(slices))
    for in_sample in itertools.combinations(range(slices), slices // 2):
        out_sample = tuple(sorted(all_slice_indices - set(in_sample)))
        is_scores = {
            key: statistics.mean(
                value for index in in_sample for value in candidate_slices[index]
            )
            for key, candidate_slices in sliced.items()
        }
        ordered_is = sorted(is_scores, key=is_scores.get, reverse=True)
        if len(ordered_is) > 1 and is_scores[ordered_is[0]] == is_scores[ordered_is[1]]:
            return StatisticResult(None, ("ambiguous_pbo_ranking",))
        winner = ordered_is[0]
        oos_scores = {
            key: statistics.mean(
                value for index in out_sample for value in candidate_slices[index]
            )
            for key, candidate_slices in sliced.items()
        }
        if len(set(oos_scores.values())) != len(oos_scores):
            return StatisticResult(None, ("ambiguous_pbo_ranking",))
        ascending = sorted(oos_scores, key=oos_scores.get)
        omega = (ascending.index(winner) + 1) / (len(ascending) + 1)
        if omega <= 0.5:
            overfit += 1
        combinations += 1
    if combinations == 0:
        return StatisticResult(None, ("insufficient_pbo_sample",))
    return StatisticResult(overfit / combinations)


def benjamini_hochberg(p_values: Mapping[str, Any], *, alpha: float) -> FDRResult:
    """Return hypotheses rejected by the Benjamini-Hochberg step-up rule."""
    if not p_values:
        return FDRResult((), ("missing_fdr_evidence",))
    values: dict[str, float] = {}
    for key, raw in p_values.items():
        if not isinstance(key, str) or not key:
            return FDRResult((), ("non_finite_fdr_input",))
        value = _finite_float(raw)
        if value is None:
            return FDRResult((), ("non_finite_fdr_input",))
        values[key] = value
    safe_alpha = _finite_float(alpha)
    if (
        safe_alpha is None
        or not 0 < safe_alpha < 1
        or any(value < 0 or value > 1 for value in values.values())
    ):
        return FDRResult((), ("invalid_fdr_p_value",))
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    largest = 0
    for rank, (_, p_value) in enumerate(ordered, start=1):
        if p_value <= safe_alpha * rank / len(ordered):
            largest = rank
    rejected = tuple(sorted(key for key, _ in ordered[:largest]))
    return FDRResult(rejected)


def build_gate_artifact(
    *,
    experiment_id: str,
    run_id: str,
    config_hash: str,
    data_hash: str,
    selection: SelectionResult,
    parameter_provenance: Mapping[str, str],
    sealed_oos: SealedOOS,
    pit_evidence: PITEvidence,
    accounting: Mapping[str, Any],
    dsr: StatisticResult,
    pbo: StatisticResult,
    fdr: FDRResult,
    economic_edge_bps: Any,
    fold_metrics: Sequence[Mapping[str, Any]],
    baselines: Mapping[str, float],
    execution_cost: Mapping[str, float],
    random_baseline: Mapping[str, int],
    cost_stress: Mapping[str, float],
    registered_information_cutoff: datetime | None,
    precondition_reasons: Sequence[str],
    config: HonestGateConfig,
    sealed_oos_artifact_id: int | None = None,
) -> GateArtifact:
    reasons = [
        reason for reason in precondition_reasons if isinstance(reason, str) and reason
    ]
    if len(reasons) != len(precondition_reasons):
        reasons.append("invalid_evidence_mapping")
    reasons.extend(
        validate_pit_evidence(
            pit_evidence,
            expected_manifest_hash=data_hash,
            registered_information_cutoff=registered_information_cutoff,
        )
    )

    safe_selection, selection_valid = _safe_selection(selection, parameter_provenance)
    safe_accounting, accounting_valid = _safe_accounting(accounting)
    safe_dsr, dsr_valid = _safe_statistic(dsr)
    safe_pbo, pbo_valid = _safe_statistic(pbo)
    safe_fdr, fdr_valid = _safe_fdr(fdr)
    reasons.extend(safe_dsr["reason_codes"])
    reasons.extend(safe_pbo["reason_codes"])
    reasons.extend(safe_fdr["reason_codes"])
    if not selection_valid:
        reasons.append("invalid_selection_evidence")
    if not all((accounting_valid, dsr_valid, pbo_valid, fdr_valid)):
        reasons.append("invalid_evidence_mapping")

    dsr_value = safe_dsr["value"]
    pbo_value = safe_pbo["value"]
    if dsr_value is not None and dsr_value < config.dsr_probability_threshold:
        reasons.append("dsr_below_threshold")
    if pbo_value is not None and pbo_value > config.pbo_max:
        reasons.append("pbo_above_threshold")
    if (
        not safe_fdr["reason_codes"]
        and safe_selection["selected_parameter"] not in safe_fdr["rejected"]
    ):
        reasons.append("fdr_not_significant")

    safe_economic_edge = _json_number(economic_edge_bps)
    if (
        safe_economic_edge is None
        or safe_economic_edge < config.economic_triviality_floor_bps
    ):
        reasons.append("economic_edge_below_minimum")

    baseline_keys = set(baselines)
    expected_baselines = set(config.baseline_names)
    if (set(REQUIRED_BASELINES) | expected_baselines) - baseline_keys:
        reasons.append("missing_required_baseline")
    if baseline_keys - expected_baselines:
        reasons.append("baseline_provenance_mismatch")

    safe_oos_metrics, oos_metrics_valid = _safe_numeric_mapping(sealed_oos.metrics)
    safe_baselines, baselines_valid = _safe_numeric_mapping(baselines)
    oos_net = safe_oos_metrics.get("net_return")
    baseline_values = list(safe_baselines.values())
    if (
        not oos_metrics_valid
        or not baselines_valid
        or oos_net is None
        or len(baseline_values) != len(baselines)
        or any(value is None or oos_net <= value for value in baseline_values)
    ):
        reasons.append("baseline_not_beaten")

    expected_execution_cost = {
        "fee_bps": config.taker_bps,
        "half_spread_bps": config.half_spread_bps,
        "slippage_bps": config.slippage_bps,
    }
    safe_execution_cost, execution_cost_valid = _safe_numeric_mapping(execution_cost)
    if not execution_cost_valid or safe_execution_cost != expected_execution_cost:
        reasons.append("execution_cost_mismatch")

    expected_random_baseline = {
        "seed": config.random_baseline_seed,
        "repetitions": config.random_baseline_repetitions,
    }
    safe_random_baseline: dict[str, int | None] = {}
    random_baseline_valid = isinstance(random_baseline, Mapping)
    for key, value in random_baseline.items():
        if not isinstance(key, str):
            random_baseline_valid = False
            continue
        valid_value = type(value) is int and _json_number(value) is not None
        safe_random_baseline[key] = value if valid_value else None
        random_baseline_valid = random_baseline_valid and valid_value
    if not random_baseline_valid or safe_random_baseline != expected_random_baseline:
        reasons.append("random_baseline_provenance_mismatch")

    expected_stress_keys = {
        str(float(multiplier)) for multiplier in config.cost_stress_multipliers
    }
    if set(cost_stress) != expected_stress_keys:
        reasons.append("cost_stress_provenance_mismatch")
    safe_cost_stress, cost_stress_valid = _safe_numeric_mapping(cost_stress)
    stress_values = list(safe_cost_stress.values())
    if (
        not cost_stress_valid
        or len(stress_values) != len(cost_stress)
        or any(value is None or value <= 0 for value in stress_values)
    ):
        reasons.append("cost_stress_failed")
    baseline_stress = safe_cost_stress.get("1.0")
    baseline_matches = (
        baseline_stress is not None
        and oos_net is not None
        and math.isclose(baseline_stress, oos_net, rel_tol=0.0, abs_tol=1e-12)
    )
    if not baseline_matches:
        reasons.append("cost_stress_baseline_mismatch")

    safe_observed_mdd = safe_oos_metrics.get("max_drawdown_pct")
    if (
        safe_observed_mdd is None
        or safe_observed_mdd < 0
        or safe_observed_mdd > config.mdd_target_pct
    ):
        reasons.append("mdd_target_exceeded")

    pit_payload: dict[str, Any] = {
        "manifest_hash": (
            pit_evidence.manifest_hash
            if isinstance(pit_evidence.manifest_hash, str)
            else None
        ),
        "manifest_timestamp": _utc_iso(pit_evidence.manifest_timestamp),
        "max_observation_timestamp": _utc_iso(pit_evidence.max_observation_timestamp),
        "information_cutoff": _utc_iso(pit_evidence.information_cutoff),
        "registered_information_cutoff": _utc_iso(
            registered_information_cutoff,
            tolerate_naive_utc=True,
        ),
    }
    safe_fold_metrics, fold_metrics_valid = _safe_fold_metrics(fold_metrics)
    if not fold_metrics_valid:
        reasons.append("invalid_fold_metrics")
    if not all(
        (
            oos_metrics_valid,
            baselines_valid,
            execution_cost_valid,
            random_baseline_valid,
            cost_stress_valid,
        )
    ):
        reasons.append("invalid_evidence_mapping")

    if sealed_oos_artifact_id is not None and (
        type(sealed_oos_artifact_id) is not int or sealed_oos_artifact_id <= 0
    ):
        reasons.append("invalid_evidence_mapping")
        sealed_oos_artifact_id = None
    safe_thresholds, thresholds_valid = _safe_evidence_value(config.to_dict())
    if not thresholds_valid:
        reasons.append("invalid_evidence_mapping")

    reason_codes = _reasons(*reasons)
    payload = {
        "schema_version": "honest_offline_gate.v1",
        "experiment_id": experiment_id,
        "run_id": run_id,
        "config_hash": config_hash,
        "data_hash": data_hash,
        "sealed_oos_artifact_id": sealed_oos_artifact_id,
        "selected_parameter": safe_selection["selected_parameter"],
        "selection": safe_selection,
        "accounting": safe_accounting,
        "dsr": safe_dsr,
        "pbo": safe_pbo,
        "fdr": safe_fdr,
        "economic_edge_bps": safe_economic_edge,
        "fold_metrics": safe_fold_metrics,
        "oos_metrics": safe_oos_metrics,
        "baselines": safe_baselines,
        "execution_cost": safe_execution_cost,
        "random_baseline": safe_random_baseline,
        "cost_stress": safe_cost_stress,
        "mdd": {
            "target_pct": _json_number(config.mdd_target_pct),
            "observed_pct": safe_observed_mdd,
        },
        "pit": pit_payload,
        "thresholds": safe_thresholds,
        "promotable": not reason_codes,
        "reason_codes": list(reason_codes),
    }
    artifact_hash = canonical_sha256(payload)
    return GateArtifact(_payload=deepcopy(payload), artifact_hash=artifact_hash)
