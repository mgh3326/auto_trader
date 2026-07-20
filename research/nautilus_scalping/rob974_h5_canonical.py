"""ROB-983 (H5, CP6) -- canonical semantic JSON and presentation-independent
hash.

JSON is the SOLE semantic source: every numeric leaf is an exact finite
``float``/``int`` (``bool``/``Decimal``/subclass/NaN/Inf rejected, no
string-repair casts). The one producer-approved derived-undefined field is
per-bucket profit factor (``pf``): a zero-loss-with-profit bucket is
mathematically ``+inf`` and a zero-loss-zero-profit bucket is ``nan`` --
neither is valid JSON, so both are sanitized to ``null`` plus a stable
``pf_reason`` code before they ever reach the canonical tree. Any OTHER
non-finite float reaching the canonical validator is a bug, not a
legitimate case, and is rejected.

Every dict-shaped field sourced from CP1-CP5 (status counts, fold-keyed
ratios, symbol/pair/exit-reason attribution, rejection histograms) is
re-keyed into the REGISTERED domain order (``STRATEGIES``, ``FOLD_IDS``,
``PATH_SCENARIOS``, ``S3_SYMBOLS``/``S4_PAIRS``, ``S3_EXIT_REASONS``/
``S4_EXIT_REASONS``, ``config_ids_for``) rather than forwarded as-is --
permuting the INPUT dict's construction order can never change the output
bytes. ``reasons``/``incomplete_reasons`` are already alphabetically sorted
tuples from CP1-CP5 and pass through unchanged (still a fixed, explicit
order).

Trades feeding CP6 canonicalization carry a fixture-derived chronological
key (``fold_id``, ``config_id``, ``entry_ts``, ``exit_ts``, ``dimension``,
``path_scenario``) -- ``actual_h4_ledger_key`` is intentionally the
``NOT_EVALUATED`` sentinel in CP6/CP7; only CP8's actual H4 integration may
bind it to a real ledger-derived key. A collision in the fixture
chronological key (two trades mapping to the same key) makes ordering
ambiguous and is rejected rather than silently tie-broken.

This module never touches a clock, the filesystem, or directory iteration
order -- it is a pure function of already-validated CP1-CP5 dataclass
instances, so wall-clock/mtime/directory-order can never enter the hash.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rob974_h5_contracts import (
    FOLD_IDS,
    PATH_SCENARIOS,
    STRATEGIES,
    CampaignEnvelope,
    H5InputError,
    H6AAccountingSeal,
    MetricTrade,
    config_ids_for,
)
from rob974_h5_dual_evidence import (
    PathInvocationEvidence,
    PboEvidence,
    UniqueGeneratorEvidence,
)
from rob974_h5_gates import CommonGateResult
from rob974_h5_s3 import S3FalsificationResult
from rob974_h5_s4 import (
    CampaignDecisionResult,
    S4FalsificationResult,
    S4PairExecutorState,
    compute_direct_verdict,
)

__all__ = [
    "ACTUAL_H4_LEDGER_KEY_NOT_EVALUATED",
    "CLOSED_STATUS_ORDER",
    "SCHEMA_VERSION",
    "StrategyCanonicalInputs",
    "build_canonical_scorecard",
    "canonical_json_bytes",
    "chronological_key",
    "hash_canonical_bytes",
    "sort_trades_chronologically",
]

SCHEMA_VERSION = "h5_scorecard_v1"
ACTUAL_H4_LEDGER_KEY_NOT_EVALUATED = "NOT_EVALUATED"
CLOSED_STATUS_ORDER: tuple[str, ...] = ("completed", "rejected", "crashed", "timeout")

_PF_REASON_INFINITE = "pf_infinite_zero_loss_with_profit"
_PF_REASON_UNDEFINED = "pf_undefined_zero_loss_and_zero_profit"


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise H5InputError(reason)


def _exact_float(value: Any, reason: str) -> float:
    _require(type(value) is float and math.isfinite(value), reason)
    return value


def _exact_int(value: Any, reason: str) -> int:
    _require(type(value) is int, reason)
    return value


def _ordered_by(keys_in_order: Sequence[str], source: Mapping[str, Any]) -> dict:
    """Rebuild ``source`` iterating ``keys_in_order`` -- a permutation of
    ``source``'s own construction order can never change the result."""
    return {k: source[k] for k in keys_in_order if k in source}


def _validate_canonical_value(value: Any, path: str) -> None:
    """Defense-in-depth recursive guard applied to raw-dict/list structures
    this module assembles directly (attribution buckets, dual-evidence
    entries, campaign-decision view) -- upstream CP1-CP5 dataclasses already
    validate their own primitive fields at construction time."""
    if value is None or type(value) is str:
        return
    if type(value) is bool:
        return
    if type(value) is int:
        return
    if type(value) is float:
        _require(math.isfinite(value), f"canonical_non_finite_float_at_{path}")
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _require(type(k) is str, f"canonical_non_string_key_at_{path}")
            _validate_canonical_value(v, f"{path}.{k}")
        return
    if isinstance(value, list | tuple):
        for i, v in enumerate(value):
            _validate_canonical_value(v, f"{path}[{i}]")
        return
    raise H5InputError(f"canonical_unsupported_type_at_{path}:{type(value)!r}")


def sanitize_bucket(bucket: Mapping[str, Any]) -> dict:
    """Sanitize one attribution bucket (as produced by CP4/CP5's internal
    ``_bucket`` helper): ``pf`` may be ``+inf``/``nan`` when a bucket has no
    losses/no profit -- convert to ``null`` plus a stable ``pf_reason``,
    never leaving a non-finite float in the canonical tree."""
    trades = _exact_int(bucket["trades"], "bucket_trades_malformed")
    e17_bps = bucket["e17_bps"]
    e0_bps = bucket["e0_bps"]
    pf = bucket["pf"]
    avg_holding_minutes = bucket["avg_holding_minutes"]
    if e17_bps is not None:
        e17_bps = _exact_float(e17_bps, "bucket_e17_bps_malformed")
    if e0_bps is not None:
        e0_bps = _exact_float(e0_bps, "bucket_e0_bps_malformed")
    if avg_holding_minutes is not None:
        avg_holding_minutes = _exact_float(
            avg_holding_minutes, "bucket_avg_holding_minutes_malformed"
        )
    pf_reason: str | None = None
    if pf is not None:
        _require(type(pf) is float, "bucket_pf_malformed")
        if math.isinf(pf):
            pf_reason = _PF_REASON_INFINITE
            pf = None
        elif math.isnan(pf):
            pf_reason = _PF_REASON_UNDEFINED
            pf = None
    return {
        "trades": trades,
        "e17_bps": e17_bps,
        "e0_bps": e0_bps,
        "pf": pf,
        "pf_reason": pf_reason,
        "avg_holding_minutes": avg_holding_minutes,
    }


def canonicalize_by_exit_reason(
    by_exit_reason: Mapping[str, Mapping[str, Any]], *, exit_reason_order: Sequence[str]
) -> dict:
    ordered = _ordered_by(exit_reason_order, by_exit_reason)
    return {reason: sanitize_bucket(bucket) for reason, bucket in ordered.items()}


def canonicalize_by_dimension(
    by_dimension: Mapping[str, Mapping[str, Any]], *, dimension_order: Sequence[str]
) -> dict:
    ordered = _ordered_by(dimension_order, by_dimension)
    return {dim: sanitize_bucket(bucket) for dim, bucket in ordered.items()}


def chronological_key(trade: MetricTrade) -> tuple:
    return (
        trade.fold_id,
        trade.config_id,
        trade.entry_ts,
        trade.exit_ts,
        trade.dimension,
        trade.path_scenario,
    )


def sort_trades_chronologically(
    trades: tuple[MetricTrade, ...],
) -> tuple[MetricTrade, ...]:
    """Sort once by the fixture-derived chronological key, rejecting a
    collision (two trades mapping to the same key) rather than silently
    breaking the tie with an arbitrary stable-sort artifact."""
    keys = [chronological_key(t) for t in trades]
    _require(len(set(keys)) == len(keys), "chronological_key_collision")
    return tuple(
        t for _, t in sorted(zip(keys, trades, strict=True), key=lambda kt: kt[0])
    )


@dataclass(frozen=True, slots=True)
class StrategyCanonicalInputs:
    strategy: str
    common_gates: CommonGateResult
    falsification: S3FalsificationResult | S4FalsificationResult
    direct_verdict: str
    exit_reason_order: tuple[str, ...]
    dimension_order: tuple[str, ...]
    unique_by_key: dict[tuple[str, str], UniqueGeneratorEvidence]
    paths_by_key: dict[tuple[str, str, str], PathInvocationEvidence]
    pbo: PboEvidence | None
    pair_executor_state: S4PairExecutorState | None = None


def _canonicalize_common_gates(gates: CommonGateResult) -> dict:
    return {
        "passed": gates.passed,
        "reasons": list(gates.reasons),
        "pooled_e17_bps": (
            _exact_float(gates.pooled_e17_bps, "gates_pooled_e17_malformed")
            if gates.pooled_e17_bps is not None
            else None
        ),
        "pf17": (
            None
            if gates.pf17 is None or not math.isfinite(gates.pf17)
            else _exact_float(gates.pf17, "gates_pf17_malformed")
        ),
        "pf17_reason": (
            None
            if gates.pf17 is None
            else (
                None
                if math.isfinite(gates.pf17)
                else (
                    _PF_REASON_INFINITE
                    if math.isinf(gates.pf17)
                    else _PF_REASON_UNDEFINED
                )
            )
        ),
        "positive_fold_count": _exact_int(
            gates.positive_fold_count, "gates_positive_fold_count_malformed"
        ),
        "monthly_concentration": (
            _exact_float(gates.monthly_concentration, "gates_concentration_malformed")
            if gates.monthly_concentration is not None
            else None
        ),
        "e22_bps": (
            _exact_float(gates.e22_bps, "gates_e22_malformed")
            if gates.e22_bps is not None
            else None
        ),
        "e0_bps": (
            _exact_float(gates.e0_bps, "gates_e0_malformed")
            if gates.e0_bps is not None
            else None
        ),
        "observed_win_rate": (
            _exact_float(gates.observed_win_rate, "gates_win_rate_malformed")
            if gates.observed_win_rate is not None
            else None
        ),
        "weighted_pbe": (
            _exact_float(gates.weighted_pbe, "gates_weighted_pbe_malformed")
            if gates.weighted_pbe is not None
            else None
        ),
        "win_margin": (
            _exact_float(gates.win_margin, "gates_win_margin_malformed")
            if gates.win_margin is not None
            else None
        ),
        "fold_trade_counts": {
            fold_id: _exact_int(
                gates.fold_trade_counts[fold_id], "gates_fold_trade_count_malformed"
            )
            for fold_id in FOLD_IDS
            if fold_id in gates.fold_trade_counts
        },
    }


def _canonicalize_s3_falsification(result: S3FalsificationResult) -> dict:
    return {
        "passed": result.passed,
        "reasons": list(result.reasons),
        "incomplete_reasons": list(result.incomplete_reasons),
        "pooled_timeout_ratio": _exact_float(
            result.pooled_timeout_ratio, "s3_pooled_timeout_ratio_malformed"
        ),
        "fold_timeout_ratios": {
            fold_id: _exact_float(
                result.fold_timeout_ratios[fold_id], "s3_fold_timeout_ratio_malformed"
            )
            for fold_id in FOLD_IDS
            if fold_id in result.fold_timeout_ratios
        },
        "bullish_long_e22_bps": (
            _exact_float(result.bullish_long_e22_bps, "s3_bullish_e22_malformed")
            if result.bullish_long_e22_bps is not None
            else None
        ),
        "first_4h_sl_dependence": (
            _exact_float(result.first_4h_sl_dependence, "s3_sl_dependence_malformed")
            if result.first_4h_sl_dependence is not None
            else None
        ),
        "attribution": {
            "by_exit_reason": canonicalize_by_exit_reason(
                result.attribution["by_exit_reason"],
                exit_reason_order=("TP", "SL", "THESIS_EXIT", "TIMEOUT"),
            ),
            "by_symbol": canonicalize_by_dimension(
                result.attribution["by_symbol"],
                dimension_order=("XRPUSDT", "DOGEUSDT", "SOLUSDT"),
            ),
        },
    }


def _canonicalize_s4_falsification(result: S4FalsificationResult) -> dict:
    return {
        "passed": result.passed,
        "reasons": list(result.reasons),
        "incomplete_reasons": list(result.incomplete_reasons),
        "pooled_timeout_ratio": _exact_float(
            result.pooled_timeout_ratio, "s4_pooled_timeout_ratio_malformed"
        ),
        "fold_timeout_ratios": {
            fold_id: _exact_float(
                result.fold_timeout_ratios[fold_id], "s4_fold_timeout_ratio_malformed"
            )
            for fold_id in FOLD_IDS
            if fold_id in result.fold_timeout_ratios
        },
        "high_market_return_e22_bps": (
            _exact_float(
                result.high_market_return_e22_bps, "s4_high_market_return_e22_malformed"
            )
            if result.high_market_return_e22_bps is not None
            else None
        ),
        "correlation": (
            _exact_float(result.correlation, "s4_correlation_malformed")
            if result.correlation is not None
            else None
        ),
        "pair_concentration": (
            _exact_float(result.pair_concentration, "s4_pair_concentration_malformed")
            if result.pair_concentration is not None
            else None
        ),
        "attribution": {
            "by_exit_reason": canonicalize_by_exit_reason(
                result.attribution["by_exit_reason"],
                exit_reason_order=("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT"),
            ),
            "by_pair": canonicalize_by_dimension(
                result.attribution["by_pair"],
                dimension_order=("XRP-DOGE", "XRP-SOL", "DOGE-SOL"),
            ),
        },
    }


def _canonicalize_dual_evidence(
    *,
    strategy: str,
    unique_by_key: Mapping[tuple[str, str], UniqueGeneratorEvidence],
    paths_by_key: Mapping[tuple[str, str, str], PathInvocationEvidence],
) -> list[dict]:
    entries: list[dict] = []
    for config_id in config_ids_for(strategy):
        for fold_id in FOLD_IDS:
            unique = unique_by_key.get((config_id, fold_id))
            if unique is None:
                continue
            paths: list[dict] = []
            for path_scenario in PATH_SCENARIOS:
                path = paths_by_key.get((config_id, fold_id, path_scenario))
                if path is None:
                    continue
                paths.append(
                    {
                        "path_scenario": path_scenario,
                        "unique_evidence_hash": path.unique_evidence_hash,
                        "unique_evidence_accepted_count": _exact_int(
                            path.unique_evidence_accepted_count,
                            "path_unique_accepted_count_malformed",
                        ),
                        "engine_input_hash": path.engine_input_hash,
                        "engine_input_count": _exact_int(
                            path.engine_input_count, "path_engine_input_count_malformed"
                        ),
                        "no_trade_reason_counts": {
                            k: _exact_int(v, "path_no_trade_reason_count_malformed")
                            for k, v in sorted(path.no_trade_reason_counts.items())
                        },
                        "ledger_status": path.ledger_status,
                        "trade_count": _exact_int(
                            path.trade_count, "path_trade_count_malformed"
                        ),
                        "artifact_hash": path.artifact_hash,
                    }
                )
            entries.append(
                {
                    "config_id": config_id,
                    "fold_id": fold_id,
                    "accepted": _exact_int(
                        unique.accepted, "unique_accepted_malformed"
                    ),
                    "rejected": _exact_int(
                        unique.rejected, "unique_rejected_malformed"
                    ),
                    "accepted_input_hash": unique.accepted_input_hash,
                    "rejection_reason_histogram": {
                        k: _exact_int(v, "unique_rejection_histogram_value_malformed")
                        for k, v in sorted(unique.rejection_reason_histogram.items())
                    },
                    "paths": paths,
                }
            )
    return entries


def _canonicalize_pbo(pbo: PboEvidence | None) -> dict | None:
    if pbo is None:
        return None
    return {
        "strategy": pbo.strategy,
        "config_count": _exact_int(pbo.config_count, "pbo_config_count_malformed"),
        "day_count": _exact_int(pbo.day_count, "pbo_day_count_malformed"),
        "slices": _exact_int(pbo.slices, "pbo_slices_malformed"),
        "scenario_name": pbo.scenario_name,
        "value": (
            _exact_float(pbo.value, "pbo_value_malformed")
            if pbo.value is not None
            else None
        ),
        "reason_codes": sorted(pbo.reason_codes),
        "source_hash": pbo.source_hash,
        "input_hash": pbo.input_hash,
        "artifact_hash": pbo.artifact_hash,
    }


def _canonicalize_pair_executor_state(state: S4PairExecutorState) -> dict:
    return {
        "volatility_percentile": state.volatility_percentile,
        "volatility_percentile_provenance": state.volatility_percentile_provenance,
        "pair_executor_state": state.pair_executor_state,
        "order_count": state.order_count,
        "residual_count": state.residual_count,
        "pair_exec_fail_count": state.pair_exec_fail_count,
        "readiness": state.readiness,
        "demo_eligible": state.demo_eligible,
    }


def _canonicalize_strategy(inputs: StrategyCanonicalInputs) -> dict:
    _require(
        inputs.strategy in STRATEGIES, "strategy_canonical_inputs_strategy_unknown"
    )
    if inputs.strategy == "S3":
        falsification = _canonicalize_s3_falsification(inputs.falsification)
    else:
        falsification = _canonicalize_s4_falsification(inputs.falsification)
    entry: dict[str, Any] = {
        "common_gates": _canonicalize_common_gates(inputs.common_gates),
        "falsification": falsification,
        "dual_evidence": _canonicalize_dual_evidence(
            strategy=inputs.strategy,
            unique_by_key=inputs.unique_by_key,
            paths_by_key=inputs.paths_by_key,
        ),
        "pbo": _canonicalize_pbo(inputs.pbo),
        "direct_verdict": inputs.direct_verdict,
    }
    if inputs.pair_executor_state is not None:
        entry["pair_executor_state"] = _canonicalize_pair_executor_state(
            inputs.pair_executor_state
        )
    return entry


def build_canonical_scorecard(
    *,
    envelope: CampaignEnvelope,
    h6a_seal: H6AAccountingSeal,
    envelope_ok: bool,
    envelope_incomplete_reasons: tuple[str, ...],
    s3_inputs: StrategyCanonicalInputs,
    s4_inputs: StrategyCanonicalInputs,
    campaign_decision: CampaignDecisionResult,
) -> dict:
    """Assemble the full canonical scorecard tree in explicit, hardcoded key
    order. Never forwards a caller-supplied dict as-is -- every dict-shaped
    field is rebuilt from typed sources in registered domain order, so
    permuting how a caller constructed an upstream mapping can never change
    the resulting bytes.

    D13 fix (adversarial verify R1, finding 4): each strategy's
    ``direct_verdict`` and the campaign decision's embedded verdicts are
    RECOMPUTED here from the actual ``common_gates``/``falsification``
    results and compared against the caller-supplied values -- a forged or
    stale verdict (e.g. "historical_pass" alongside a failing/incomplete
    gate result) is rejected rather than silently canonicalized."""
    recomputed_s3_verdict = compute_direct_verdict(
        incomplete_reasons=s3_inputs.falsification.incomplete_reasons,
        hard_gate_reasons=s3_inputs.common_gates.reasons
        + s3_inputs.falsification.reasons,
    )
    _require(
        recomputed_s3_verdict == s3_inputs.direct_verdict,
        "canonical_s3_direct_verdict_forged_or_stale",
    )
    recomputed_s4_verdict = compute_direct_verdict(
        incomplete_reasons=s4_inputs.falsification.incomplete_reasons,
        hard_gate_reasons=s4_inputs.common_gates.reasons
        + s4_inputs.falsification.reasons,
    )
    _require(
        recomputed_s4_verdict == s4_inputs.direct_verdict,
        "canonical_s4_direct_verdict_forged_or_stale",
    )
    _require(
        campaign_decision.s3_direct_verdict == recomputed_s3_verdict,
        "canonical_campaign_decision_s3_verdict_mismatch",
    )
    _require(
        campaign_decision.s4_direct_verdict == recomputed_s4_verdict,
        "canonical_campaign_decision_s4_verdict_mismatch",
    )

    lineage = {
        "full_campaign_hash": envelope.full_campaign_hash,
        "campaign_run_id": envelope.campaign_run_id,
        "parent_corpus_hash": envelope.parent_corpus_hash,
        "parent_projection_hash": envelope.parent_projection_hash,
        "feature_contract_hash": envelope.feature_contract_hash,
        "strategy_contract_hashes": {
            strategy: envelope.strategy_contract_hashes[strategy]
            for strategy in STRATEGIES
        },
        "h4_runner_source_hash": envelope.h4_runner_source_hash,
        "h4_pbo_source_hash": envelope.h4_pbo_source_hash,
        "h2_engine_source_hash": envelope.h2_engine_source_hash,
        "h3_generator_source_hash": envelope.h3_generator_source_hash,
        "run_schema_version": envelope.run_schema_version,
        "generator_version": envelope.generator_version,
        "expected_experiment_ids": list(envelope.expected_experiment_ids),
        "h6a_trial_accounting_hash": envelope.h6a_trial_accounting_hash,
        "actual_h4_ledger_key": ACTUAL_H4_LEDGER_KEY_NOT_EVALUATED,
    }
    h6a_accounting = {
        "expected_total": _exact_int(
            h6a_seal.expected_total, "h6a_expected_total_malformed"
        ),
        "registered_total": _exact_int(
            h6a_seal.registered_total, "h6a_registered_total_malformed"
        ),
        "primary_attempts": _exact_int(
            h6a_seal.primary_attempts, "h6a_primary_attempts_malformed"
        ),
        "status_counts": {
            status: _exact_int(
                h6a_seal.status_counts[status], "h6a_status_count_malformed"
            )
            for status in CLOSED_STATUS_ORDER
        },
        "retry_attempts": _exact_int(
            h6a_seal.retry_attempts, "h6a_retry_attempts_malformed"
        ),
        "accounting_complete": h6a_seal.accounting_complete,
        "performance_usable": h6a_seal.performance_usable,
        "trial_accounting_hash": h6a_seal.trial_accounting_hash,
        "reason_codes": sorted(h6a_seal.reason_codes),
    }
    envelope_validation = {
        "ok": envelope_ok,
        "incomplete_reasons": sorted(envelope_incomplete_reasons),
    }
    strategies = {
        "S3": _canonicalize_strategy(s3_inputs),
        "S4": _canonicalize_strategy(s4_inputs),
    }
    campaign = {
        "campaign_decision": campaign_decision.campaign_decision,
        "campaign_historical_verdict": campaign_decision.campaign_historical_verdict,
        "s3_direct_verdict": campaign_decision.s3_direct_verdict,
        "s4_direct_verdict": campaign_decision.s4_direct_verdict,
        "demo_candidate": campaign_decision.demo_candidate,
        "historical_preferred": campaign_decision.historical_preferred,
        "s4_observable_superiority": campaign_decision.s4_observable_superiority,
    }
    scorecard = {
        "schema_version": SCHEMA_VERSION,
        "lineage": lineage,
        "h6a_accounting": h6a_accounting,
        "envelope_validation": envelope_validation,
        "strategies": strategies,
        "campaign_decision": campaign,
    }
    _validate_canonical_value(scorecard, "scorecard")
    return scorecard


def canonical_json_bytes(scorecard: Mapping[str, Any]) -> bytes:
    """Deterministic, presentation-independent byte encoding: fixed key
    order (as already assembled by ``build_canonical_scorecard``), compact
    separators, ``allow_nan=False`` (a raw NaN/Inf slipping through raises
    rather than silently producing non-portable JSON)."""
    return json.dumps(
        scorecard,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=False,
        separators=(",", ":"),
    ).encode("utf-8")


def hash_canonical_bytes(canonical_bytes: bytes) -> str:
    return hashlib.sha256(canonical_bytes).hexdigest()
