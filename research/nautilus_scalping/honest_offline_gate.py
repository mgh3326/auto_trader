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
from dataclasses import asdict, dataclass, field
from datetime import datetime
from statistics import NormalDist
from typing import Any

from app.services.research_canonical_hash import canonical_sha256

try:
    from .frozen_config import CampaignConfig
except ImportError:  # Top-level imports used by the isolated research test suite.
    from frozen_config import CampaignConfig

REQUIRED_BASELINES = ("cash", "btc_eth_equal_weight", "same_turnover_random")
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
    returns: tuple[float, ...]
    metrics: Mapping[str, float]


HonestGateConfig = CampaignConfig


@dataclass(frozen=True)
class GateArtifact:
    schema_version: str
    experiment_id: str
    run_id: str
    config_hash: str
    data_hash: str
    selected_parameter: str
    accounting: Mapping[str, Any]
    dsr: Mapping[str, Any]
    pbo: Mapping[str, Any]
    fdr: Mapping[str, Any]
    economic_edge_bps: float
    fold_metrics: tuple[Mapping[str, Any], ...]
    oos_metrics: Mapping[str, float]
    baselines: Mapping[str, float]
    cost_stress: Mapping[str, float]
    mdd: Mapping[str, float]
    pit: Mapping[str, Any]
    thresholds: Mapping[str, Any]
    promotable: bool
    reason_codes: tuple[str, ...]
    artifact_hash: str = field(compare=True)

    @property
    def primary_reason(self) -> str:
        return self.reason_codes[0] if self.reason_codes else "ok"

    def to_metrics(self) -> dict[str, Any]:
        metrics = asdict(self)
        metrics["pit"] = {
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in metrics["pit"].items()
        }
        return metrics


def _reasons(*codes: str) -> tuple[str, ...]:
    return tuple(sorted({code for code in codes if code}))


def _is_finite(values: Sequence[float]) -> bool:
    return all(math.isfinite(float(value)) for value in values)


def select_parameters(candidates: Sequence[SelectionCandidate]) -> SelectionResult:
    """Rank by validation evidence only; sealed OOS is absent by construction."""
    if not candidates:
        raise ValueError("selection requires at least one candidate")
    keys = [candidate.parameter_key for candidate in candidates]
    if any(not key for key in keys) or len(set(keys)) != len(keys):
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
    evidence: PITEvidence, *, expected_manifest_hash: str
) -> tuple[str, ...]:
    reasons: list[str] = []
    if not evidence.manifest_hash or evidence.manifest_timestamp is None:
        reasons.append("missing_pit_manifest")
    if evidence.information_cutoff is None:
        reasons.append("missing_information_cutoff")
    if evidence.manifest_hash and evidence.manifest_hash != expected_manifest_hash:
        reasons.append("pit_manifest_hash_mismatch")

    timestamps = (
        evidence.manifest_timestamp,
        evidence.max_observation_timestamp,
        evidence.information_cutoff,
    )
    if any(value is not None and value.tzinfo is None for value in timestamps):
        reasons.append("invalid_information_cutoff")
        return _reasons(*reasons)
    cutoff = evidence.information_cutoff
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
    returns: Sequence[float],
    completed_trial_sharpes: Sequence[float],
    *,
    total_trials: int,
    min_observations: int,
) -> StatisticResult:
    """Calculate DSR using PSR against the expected maximum trial Sharpe.

    `total_trials` is supplied by the registry-backed service, not promotion
    request data. Skewness and non-excess kurtosis correct the PSR denominator.
    """
    values = tuple(float(value) for value in returns)
    trial_sharpes = tuple(float(value) for value in completed_trial_sharpes)
    if not _is_finite(values) or not _is_finite(trial_sharpes):
        return StatisticResult(None, ("non_finite_dsr_input",))
    if len(values) < min_observations or total_trials < 1:
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
    candidate_returns: Mapping[str, Sequence[float]], *, slices: int
) -> StatisticResult:
    """Calculate CSCV PBO from IS winners' relative OOS ranks."""
    if len(candidate_returns) < 2:
        return StatisticResult(None, ("insufficient_pbo_sample",))
    if slices < 4 or slices % 2:
        return StatisticResult(None, ("invalid_pbo_slices",))
    rows = {
        key: tuple(float(value) for value in values)
        for key, values in candidate_returns.items()
    }
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


def benjamini_hochberg(p_values: Mapping[str, float], *, alpha: float) -> FDRResult:
    """Return hypotheses rejected by the Benjamini-Hochberg step-up rule."""
    if not p_values:
        return FDRResult((), ("missing_fdr_evidence",))
    values = {key: float(value) for key, value in p_values.items()}
    if not _is_finite(tuple(values.values())):
        return FDRResult((), ("non_finite_fdr_input",))
    if not 0 < alpha < 1 or any(value < 0 or value > 1 for value in values.values()):
        return FDRResult((), ("invalid_fdr_p_value",))
    ordered = sorted(values.items(), key=lambda item: (item[1], item[0]))
    largest = 0
    for rank, (_, p_value) in enumerate(ordered, start=1):
        if p_value <= alpha * rank / len(ordered):
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
    sealed_oos: SealedOOS,
    pit_evidence: PITEvidence,
    accounting: Mapping[str, Any],
    dsr: StatisticResult,
    pbo: StatisticResult,
    fdr: FDRResult,
    candidate_p_value_key: str,
    economic_edge_bps: float,
    fold_metrics: Sequence[Mapping[str, Any]],
    baselines: Mapping[str, float],
    cost_stress: Mapping[str, float],
    observed_mdd_pct: float,
    config: HonestGateConfig,
) -> GateArtifact:
    reasons = list(
        validate_pit_evidence(pit_evidence, expected_manifest_hash=data_hash)
    )
    reasons.extend(dsr.reason_codes)
    reasons.extend(pbo.reason_codes)
    reasons.extend(fdr.reason_codes)
    if dsr.value is not None and dsr.value < config.dsr_probability_threshold:
        reasons.append("dsr_below_threshold")
    if pbo.value is not None and pbo.value > config.pbo_max:
        reasons.append("pbo_above_threshold")
    if not fdr.reason_codes and candidate_p_value_key not in fdr.rejected:
        reasons.append("fdr_not_significant")
    if (
        not math.isfinite(economic_edge_bps)
        or economic_edge_bps < config.economic_triviality_floor_bps
    ):
        reasons.append("economic_edge_below_minimum")

    missing_baselines = set(REQUIRED_BASELINES) - set(baselines)
    if missing_baselines:
        reasons.append("missing_required_baseline")
    else:
        oos_net = float(sealed_oos.metrics.get("net_return", float("nan")))
        if not math.isfinite(oos_net) or any(
            not math.isfinite(float(value)) or oos_net <= float(value)
            for value in baselines.values()
        ):
            reasons.append("baseline_not_beaten")
    stressed_net = float(cost_stress.get("net_return", float("nan")))
    if not math.isfinite(stressed_net) or stressed_net <= 0:
        reasons.append("cost_stress_failed")
    if not math.isfinite(observed_mdd_pct) or observed_mdd_pct > config.mdd_target_pct:
        reasons.append("mdd_target_exceeded")

    reason_codes = _reasons(*reasons)
    payload = {
        "schema_version": "honest_offline_gate.v1",
        "experiment_id": experiment_id,
        "run_id": run_id,
        "config_hash": config_hash,
        "data_hash": data_hash,
        "selected_parameter": selection.selected_parameter,
        "accounting": dict(accounting),
        "dsr": asdict(dsr),
        "pbo": asdict(pbo),
        "fdr": asdict(fdr),
        "economic_edge_bps": economic_edge_bps,
        "fold_metrics": tuple(dict(metrics) for metrics in fold_metrics),
        "oos_metrics": dict(sealed_oos.metrics),
        "baselines": dict(baselines),
        "cost_stress": dict(cost_stress),
        "mdd": {"target_pct": config.mdd_target_pct, "observed_pct": observed_mdd_pct},
        "pit": asdict(pit_evidence),
        "thresholds": config.to_dict(),
        "promotable": not reason_codes,
        "reason_codes": reason_codes,
    }
    artifact_hash = canonical_sha256(payload)
    return GateArtifact(**payload, artifact_hash=artifact_hash)
