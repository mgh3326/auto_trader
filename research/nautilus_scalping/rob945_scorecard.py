"""ROB-945 (H5) -- scorecard JSON/Markdown assembly.

JSON is the sole source of truth; Markdown is a pure, deterministic render
of the already-built JSON object -- never an independent metric
computation. The final artifact hash is acyclic: the hash covers the
``scorecard_payload`` subtree only, and is then placed alongside it (never
inside it) in the outer envelope.

readiness is always ``historical_screen_only``; this module never creates
or references a ROB-905 ``validated_signal_gate.v1`` artifact/path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

import rob941_frozen_scope as frozen
from rob945_accounting_seal import (
    ACCOUNTING_INCOMPLETE_REASON,
    ScorecardInputError,
    derive_campaign_run_id,
    seal_trial_accounting,
)
from rob945_canonical_payload import to_canonical_payload
from rob945_pbo_grid import PboAuxiliaryEvidence
from rob945_scenario_metrics import FoldStabilityRow, StrategyScenarioAggregate
from rob945_signal_concurrency import StrategyConcurrencyEvidence
from rob945_verdict import evaluate_historical_verdict

from research_contracts.canonical_hash import canonical_sha256

SCHEMA_VERSION = "rob945.v1"
GENERATOR_VERSION = "rob945-h5-scorecard/1.0.0"
READINESS = "historical_screen_only"

HASH_DRIFT_REASON = "full_campaign_hash_drift"
# Kept importable under its original name for backward compatibility;
# single source of truth is now ``rob945_accounting_seal.ACCOUNTING_INCOMPLETE_REASON``.
ACCOUNTING_DRIFT_REASON = ACCOUNTING_INCOMPLETE_REASON
RUN_ID_DRIFT_REASON = "campaign_run_id_derivation_mismatch"
HASH_FORMAT_REASON = "hash_field_not_lowercase_64_hex"
FOLD_ID_SEQUENCE_REASON = "fold_id_sequence_not_canonical_contiguous"
DATASET_HASH_DRIFT_REASON = "dataset_manifest_hash_not_frozen_production_value"
SIGNAL_HASH_DRIFT_REASON = "signal_manifest_hash_not_frozen_production_value"

_LOWERCASE_HEX_64 = re.compile(r"^[0-9a-f]{64}$")

# ``ScorecardInputError`` now lives in ``rob945_accounting_seal`` (the
# lower-level sealing boundary); re-exported here so existing callers/tests
# importing it from this module keep working unchanged.
_derive_campaign_run_id = derive_campaign_run_id


_REQUIRED_STRATEGIES = ("S1", "S2")
_REQUIRED_SCENARIOS = ("base", "primary_stress", "upward_stress")
_REQUIRED_SYMBOLS = frozen.UNIVERSE  # ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")

# Frozen production walk-forward schedule (rob944_folds.generate_frozen_fold_schedule
# over the frozen full window): exactly 8 folds, fold-00..fold-07.
FROZEN_FOLD_COUNT = 8
_EXPECTED_FOLD_IDS = tuple(f"fold-{i:02d}" for i in range(FROZEN_FOLD_COUNT))

_DISCLOSURES: dict[str, Any] = {
    "account_global_collision_unmodeled": (
        "Each of the four symbols is simulated as an independent single-position "
        "stream; account-global 'at most one concurrent position' collision skips "
        "are UNMODELED here, making these historical results optimistic relative "
        "to a real single-account execution constraint."
    ),
    "demo_arbitration_required_no_rule_invented": (
        "Demo-stage cross-symbol arbitration is required before live/paper "
        "execution, but no arbitration rule is invented by this historical "
        "screen -- see signal_concurrency evidence for the empirical basis."
    ),
    "no_spread_age_lot_gates_in_historical": (
        "Historical evaluation has no bid/ask spread, data-age, or LOT_SIZE "
        "gate (1m OHLCV has no such fields); demo execution MUST reverify "
        "these before any live/paper order."
    ),
    "s1_07_footnote": (
        "S1-07 is frozen as registered even though its target TP distance "
        "(67.5bp) is below the fixed 68bp minimum-TP floor -- a legitimate, "
        "low-volatility payoff configuration for this config can produce zero "
        "trades rather than being silently dropped from the roster."
    ),
    "s2_spec_deviation_register_visible": True,
    "not_validated_signal_gate": True,
}

# I-4 final-fix: S2's spec-deviation register content (Fable ruling,
# orch-fable-answer-rob943-s2-20260717.md, Q1=A final) -- machine-visible,
# never silently expanded/invented beyond this exact statement.
_S2_SPEC_DEVIATION_STATEMENT = (
    "original three gates plus a direction-validity gate by Fable ruling, "
    "to prevent label contamination"
)
# H3 S2's exact closed 6-code pre-execution rejection set (mirrors
# rob944_walkforward.H3_GENERATOR_REJECTION_REASONS -- literal hand-verified
# duplicate, same pattern as other H5 closed-reason allowlists).
_S2_REJECTION_REASONS: tuple[str, ...] = (
    "confirmation_failed",
    "next_bar_unavailable",
    "target_direction_invalid",
    "tp_above_max",
    "tp_below_r_min_sl",
    "tp_below_abs_floor",
)


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ScorecardInputError(reason)


def _ex_btc_reference_subtotal_payload(
    aggregate: StrategyScenarioAggregate,
) -> dict[str, Any]:
    """I-4 final-fix: a reference-only 3-symbol (XRP/DOGE/SOL) subtotal,
    mechanically derived from the already-validated per-symbol rows --
    never a new pass rule. ``symbols`` preserves the frozen-universe order
    (``symbol_metrics`` is itself built in that order) minus BTCUSDT."""
    rows = [m for m in aggregate.symbol_metrics if m.symbol != "BTCUSDT"]
    trade_count = sum(m.trade_count for m in rows)
    signal_count = sum(m.signal_count for m in rows)
    net_pnl_bps = sum(m.net_pnl_bps for m in rows)
    pooled_expectancy_bps = net_pnl_bps / trade_count if trade_count else None
    return {
        "symbols": [m.symbol for m in rows],
        "trade_count": trade_count,
        "signal_count": signal_count,
        "net_pnl_bps": net_pnl_bps,
        "pooled_expectancy_bps": pooled_expectancy_bps,
        "reference_only": True,
        "has_pass_rule": False,
    }


def _scenario_aggregate_payload(aggregate: StrategyScenarioAggregate) -> dict[str, Any]:
    return {
        "scenario_name": aggregate.scenario_name,
        "trade_count": aggregate.trade_count,
        "net_expectancy_bps": aggregate.net_expectancy_bps,
        "pooled_expectancy_bps": aggregate.pooled_expectancy_bps,
        "profit_factor": aggregate.profit_factor,
        "win_rate": aggregate.win_rate,
        "net_pnl_bps": aggregate.net_pnl_bps,
        "timeout_ratio": aggregate.timeout_ratio,
        "mdd_r": aggregate.mdd_r,
        "mdd_reason": aggregate.mdd_reason,
        "monthly_concentration": aggregate.monthly_concentration,
        "monthly_concentration_reason": aggregate.monthly_concentration_reason,
        "symbol_metrics": [
            {
                "symbol": m.symbol,
                "trade_count": m.trade_count,
                "signal_count": m.signal_count,
                "net_expectancy_bps": m.net_expectancy_bps,
                "net_pnl_bps": m.net_pnl_bps,
            }
            for m in aggregate.symbol_metrics
        ],
        "incomplete": aggregate.incomplete,
        "incomplete_reason": aggregate.incomplete_reason,
        # I-4 final-fix: mechanically derived from the H4-exposed
        # no_trade_reason_counts histogram -- never a caller-authored bool.
        "no_trade_reason_counts": dict(
            sorted(aggregate.no_trade_reason_counts.items())
        ),
        "daily_stop_active_count": aggregate.no_trade_reason_counts.get(
            "daily_stop_active", 0
        ),
        "ex_btc_reference_subtotal": _ex_btc_reference_subtotal_payload(aggregate),
    }


def _scenario_trade_count_deltas_payload(
    scenarios: Mapping[str, StrategyScenarioAggregate],
) -> dict[str, int]:
    """I-4 final-fix: explicit, deterministic pairwise trade-count deltas --
    the known independent H2 3/3/2 fixture must remain VISIBLY 3/3/2 here,
    never silently path-equalized. No new pass threshold."""
    return {
        "base_minus_primary_stress": (
            scenarios["base"].trade_count - scenarios["primary_stress"].trade_count
        ),
        "primary_stress_minus_upward_stress": (
            scenarios["primary_stress"].trade_count
            - scenarios["upward_stress"].trade_count
        ),
        "base_minus_upward_stress": (
            scenarios["base"].trade_count - scenarios["upward_stress"].trade_count
        ),
    }


def _s2_spec_deviation_register_payload(
    scenarios: Mapping[str, StrategyScenarioAggregate],
) -> dict[str, Any]:
    """I-4 final-fix: S2-only machine-visible register of the frozen
    direction-validity-gate disclosure plus per-scenario/total rejection
    counts, derived from the H4-exposed no_trade_reason_counts histograms.
    S1 must not carry this key at all."""
    rejection_counts_by_scenario = {
        scenario_name: {
            reason: aggregate.no_trade_reason_counts.get(reason, 0)
            for reason in _S2_REJECTION_REASONS
        }
        for scenario_name, aggregate in scenarios.items()
    }
    total_rejection_counts = {
        reason: sum(
            aggregate.no_trade_reason_counts.get(reason, 0)
            for aggregate in scenarios.values()
        )
        for reason in _S2_REJECTION_REASONS
    }
    return {
        "statement": _S2_SPEC_DEVIATION_STATEMENT,
        "rejection_counts_by_scenario": rejection_counts_by_scenario,
        "total_rejection_counts": total_rejection_counts,
    }


def _fold_stability_payload(rows: tuple[FoldStabilityRow, ...]) -> list[dict[str, Any]]:
    return [
        {
            "fold_id": r.fold_id,
            "selected_config_id": r.selected_config_id,
            "trade_count": r.trade_count,
            "net_expectancy_bps": r.net_expectancy_bps,
            "net_pnl_bps": r.net_pnl_bps,
            "profit_factor": r.profit_factor,
            "positive": r.positive,
            "net_pnl_class": r.net_pnl_class,
        }
        for r in rows
    ]


def _concurrency_payload(evidence: StrategyConcurrencyEvidence) -> dict[str, Any]:
    return {
        "numerator": evidence.numerator,
        "denominator": evidence.denominator,
        "rate": evidence.rate,
        "reason": evidence.reason,
        "distinct_symbol_count_histogram": {
            str(bucket): count
            for bucket, count in evidence.distinct_symbol_count_histogram.items()
        },
    }


def _pbo_payload(evidence: PboAuxiliaryEvidence) -> dict[str, Any]:
    return {
        "value": evidence.value,
        "reason_codes": list(evidence.reason_codes),
        "slices": evidence.slices,
        "config_count": evidence.config_count,
        "day_count": evidence.day_count,
        "artifact_hash": evidence.artifact_hash,
    }


def _validate_and_build_strategy(
    strategy: str, evidence: Mapping[str, Any]
) -> dict[str, Any]:
    scenarios = evidence["scenarios"]
    _require(
        set(scenarios.keys()) == set(_REQUIRED_SCENARIOS),
        f"missing_or_extra_scenario_names_for_{strategy}",
    )
    # Captain precision: normalize ONCE into the exact canonical scenario
    # order -- every downstream use (payload, deltas, S2 register) consumes
    # this SAME order, so a caller supplying scenarios in a different
    # Mapping insertion order can never leak into any output ordering.
    scenarios = {name: scenarios[name] for name in _REQUIRED_SCENARIOS}
    for scenario_name, aggregate in scenarios.items():
        _require(
            aggregate.scenario_name == scenario_name,
            f"scenario_name_mismatch_for_{strategy}_{scenario_name}",
        )
        # Captain precision: exact frozen symbol ORDER, not just set
        # equality -- a duplicate/reordered symbol_metrics tuple (same set)
        # must still fail closed, so ex_btc_reference_subtotal's filtered
        # list is guaranteed to be exactly [XRP, DOGE, SOL] once each.
        symbol_order = tuple(m.symbol for m in aggregate.symbol_metrics)
        _require(
            symbol_order == _REQUIRED_SYMBOLS,
            f"missing_symbol_coverage_for_{strategy}_{scenario_name}",
        )

    fold_stability_input = evidence["fold_stability"]
    _require(
        len(fold_stability_input) == FROZEN_FOLD_COUNT,
        f"insufficient_fold_count_for_{strategy}",
    )
    fold_by_id = {row.fold_id: row for row in fold_stability_input}
    _require(
        set(fold_by_id) == set(_EXPECTED_FOLD_IDS)
        and len(fold_by_id) == FROZEN_FOLD_COUNT,
        f"{FOLD_ID_SEQUENCE_REASON}_for_{strategy}",
    )
    # normalize ONCE into the exact canonical fold-00..fold-07 order -- every
    # downstream use (payload, counts, verdict) consumes this SAME tuple, so
    # a caller supplying rows in a different order can never leak into any
    # output (including byte-level float summation order elsewhere).
    fold_stability = tuple(fold_by_id[fold_id] for fold_id in _EXPECTED_FOLD_IDS)

    # Derived from ``net_pnl_class`` (not the coarser ``row.positive``
    # bool|None), which conflates exactly-zero net PnL with genuinely
    # negative -- ``net_pnl_class`` distinguishes all four cases:
    # positive / zero / negative / undefined (no trades this fold).
    positive_oos_fold_count = sum(
        1 for row in fold_stability if row.net_pnl_class == "positive"
    )
    zero_oos_fold_count = sum(
        1 for row in fold_stability if row.net_pnl_class == "zero"
    )
    negative_oos_fold_count = sum(
        1 for row in fold_stability if row.net_pnl_class == "negative"
    )
    undefined_oos_fold_count = sum(
        1 for row in fold_stability if row.net_pnl_class is None
    )
    selected_config_frequency: dict[str, int] = {}
    for row in fold_stability:
        if row.selected_config_id:
            selected_config_frequency[row.selected_config_id] = (
                selected_config_frequency.get(row.selected_config_id, 0) + 1
            )

    # capture_valid=False: the OOS signal-capture evidence feeding this
    # strategy's scenarios/concurrency was itself latched invalid/incomplete
    # (rob945_capture.OosSignalCaptureSink) -- a genuine evidence gap.
    #
    # pbo_valid=False: the SEALED PBO GRID ITSELF was structurally invalid
    # (missing config, unequal grid, gap-invalid day, non-finite input --
    # i.e. ``rob945_pbo_grid.PboGridError`` was raised upstream). This is
    # DISTINCT from a valid PBO evidence object whose *statistical result*
    # happens to be None/insufficient/ambiguous -- that latter case is
    # explicitly report-only per the frozen contract and must NEVER gate
    # pass/fail. Structural grid invalidity is not a "result", it means no
    # PBO evidence could be produced at all -- that is incomplete.
    capture_valid = evidence.get("capture_valid", True)
    pbo_valid = evidence.get("pbo_valid", True)
    evidence_complete = capture_valid and pbo_valid
    incomplete_reason = None
    if not evidence_complete:
        incomplete_reason = (
            "capture_invalid" if not capture_valid else "pbo_grid_invalid"
        )
    verdict = evaluate_historical_verdict(
        primary_stress=scenarios["primary_stress"],
        upward_stress=scenarios["upward_stress"],
        positive_oos_fold_count=positive_oos_fold_count,
        accounting_complete=evidence_complete,
        accounting_incomplete_reason=incomplete_reason,
    )

    strategy_payload = {
        "strategy": strategy,
        "scenarios": {
            name: _scenario_aggregate_payload(agg) for name, agg in scenarios.items()
        },
        "scenario_trade_count_deltas": _scenario_trade_count_deltas_payload(scenarios),
        "fold_stability": _fold_stability_payload(fold_stability),
        "positive_oos_fold_count": positive_oos_fold_count,
        "zero_oos_fold_count": zero_oos_fold_count,
        "negative_oos_fold_count": negative_oos_fold_count,
        "undefined_oos_fold_count": undefined_oos_fold_count,
        "selected_config_frequency": dict(sorted(selected_config_frequency.items())),
        "signal_concurrency": _concurrency_payload(evidence["signal_concurrency"]),
        "pbo": _pbo_payload(evidence["pbo"]),
        "verdict": {
            "verdict": verdict.verdict,
            "readiness": verdict.readiness,
            "reason_codes": list(verdict.reason_codes),
        },
    }
    # I-4 final-fix / captain precision: S2-only spec-deviation register --
    # S1 must OMIT the key entirely (never carry it present-but-None).
    if strategy == "S2":
        strategy_payload["spec_deviation_register"] = (
            _s2_spec_deviation_register_payload(scenarios)
        )
    return strategy_payload


def _symbol_universe_payload() -> list[dict[str, Any]]:
    return [
        {"symbol": symbol, **frozen.eligibility(symbol)} for symbol in _REQUIRED_SYMBOLS
    ]


def build_scorecard(
    *,
    full_campaign_hash: str,
    full_campaign_payload: dict[str, Any],
    campaign_run_id: str,
    dataset_manifest_hash: str,
    signal_manifest_hash: str,
    accounting_report: Mapping[str, Any],
    attempt_evidence: Any,
    walkforward_results: Mapping[str, Any],
    strategies: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    for hash_value in (full_campaign_hash, dataset_manifest_hash, signal_manifest_hash):
        _require(bool(_LOWERCASE_HEX_64.match(hash_value)), HASH_FORMAT_REASON)

    recomputed_hash = canonical_sha256(to_canonical_payload(full_campaign_payload))
    _require(recomputed_hash == full_campaign_hash, HASH_DRIFT_REASON)

    expected_campaign_run_id = _derive_campaign_run_id(full_campaign_hash)
    _require(campaign_run_id == expected_campaign_run_id, RUN_ID_DRIFT_REASON)

    # Sealed H6 accounting/attempt-evidence boundary (ROB-945 Task 1): the
    # ONLY authority for H6 completeness -- never a bare
    # ``accounting_report.get("verdict") == "complete"`` string check, and
    # never a caller-trusted ``full_campaign_hash`` (re-pinned inside the
    # seal against a fresh real production envelope). A well-formed report
    # that is merely INCOMPLETE (missing/extra/mismatch/gap evidence, or a
    # primary attempt that didn't complete) is not a raise here -- it
    # propagates into ``campaign_verdict`` as ``incomplete`` below, per the
    # "malformed raises, well-formed-incomplete seals as incomplete"
    # distinction (Task 1, RED case 10).
    sealed_accounting = seal_trial_accounting(
        accounting_report=accounting_report,
        attempt_evidence=attempt_evidence,
        full_campaign_hash=full_campaign_hash,
        walkforward_results=walkforward_results,
    )
    # The seal's ground-truth envelope also carries the REAL dataset/signal
    # manifest hashes -- cross-check the caller's own top-level
    # ``dataset_manifest_hash``/``signal_manifest_hash`` arguments against
    # them (previously only format-checked, never checked for CONTENT
    # against the frozen production values -- the same "self-consistent
    # arbitrary" gap Task 1 closed for ``full_campaign_hash``).
    _require(
        dataset_manifest_hash == sealed_accounting.dataset_manifest_hash,
        DATASET_HASH_DRIFT_REASON,
    )
    _require(
        signal_manifest_hash == sealed_accounting.signal_manifest_hash,
        SIGNAL_HASH_DRIFT_REASON,
    )

    _require(
        set(strategies.keys()) == set(_REQUIRED_STRATEGIES),
        "missing_or_extra_strategy_keys",
    )

    strategy_payloads = {
        strategy: _validate_and_build_strategy(strategy, evidence)
        for strategy, evidence in strategies.items()
    }

    strategy_verdicts = [
        strategy_payloads[s]["verdict"]["verdict"] for s in _REQUIRED_STRATEGIES
    ]
    # I-5 / Task 6.1 final-fix: a top-level, sorted/deduplicated union of
    # ONLY the reasons that actually DROVE campaign_verdict -- never a
    # non-driving strategy's reasons (incomplete precedence means a
    # strategy that would merely have failed never contributes once
    # ANOTHER strategy/accounting gap already made the campaign
    # incomplete).
    campaign_reason_codes: set[str] = set()
    accounting_incomplete = not sealed_accounting.performance_usable
    if accounting_incomplete or any(v == "incomplete" for v in strategy_verdicts):
        # Campaign-level H6 accounting evidence gap and/or a strategy-level
        # incomplete both drive this verdict -- captain precision: when
        # BOTH apply simultaneously, the union of BOTH sets of reasons is
        # required (neither side alone), while any OTHER strategy's
        # historical_fail reasons remain excluded (incomplete precedence).
        campaign_verdict = "incomplete"
        if accounting_incomplete:
            campaign_reason_codes.update(sealed_accounting.reason_codes)
        for s in _REQUIRED_STRATEGIES:
            if strategy_payloads[s]["verdict"]["verdict"] == "incomplete":
                campaign_reason_codes.update(
                    strategy_payloads[s]["verdict"]["reason_codes"]
                )
    elif any(v == "historical_fail" for v in strategy_verdicts):
        campaign_verdict = "historical_fail"
        for s in _REQUIRED_STRATEGIES:
            if strategy_payloads[s]["verdict"]["verdict"] == "historical_fail":
                campaign_reason_codes.update(
                    strategy_payloads[s]["verdict"]["reason_codes"]
                )
    else:
        campaign_verdict = "historical_pass"
        # empty -- a genuine historical_pass has no driving reason.

    # I-1 final correction: denominator is always a plain int now (never
    # None/null) -- natural integer sum, no null-masking.
    overall_numerator = sum(
        strategy_payloads[s]["signal_concurrency"]["numerator"]
        for s in _REQUIRED_STRATEGIES
    )
    overall_denominator = sum(
        strategy_payloads[s]["signal_concurrency"]["denominator"]
        for s in _REQUIRED_STRATEGIES
    )

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "readiness": READINESS,
        "campaign_verdict": campaign_verdict,
        "campaign_reason_codes": sorted(campaign_reason_codes),
        "lineage": {
            "full_campaign_hash": full_campaign_hash,
            "campaign_run_id": campaign_run_id,
            "dataset_manifest_hash": dataset_manifest_hash,
            "signal_manifest_hash": signal_manifest_hash,
            # From the sealed accounting boundary -- hashes the FULL,
            # frozen-order-normalized evidence (report + all attempt
            # records), never just a bare {"verdict": "complete"} string.
            "trial_accounting_hash": sealed_accounting.trial_accounting_hash,
            "accounting_complete": sealed_accounting.accounting_complete,
            "accounting_performance_usable": sealed_accounting.performance_usable,
            "accounting_reason_codes": list(sealed_accounting.reason_codes),
        },
        "strategies": strategy_payloads,
        "signal_concurrency_overall": {
            "numerator": overall_numerator,
            "denominator": overall_denominator,
        },
        "symbol_universe": _symbol_universe_payload(),
        "disclosures": dict(_DISCLOSURES),
    }

    # JSON-safe canonical payload -- non-finite floats become the stable
    # sentinel string (e.g. a legitimate +Inf profit_factor) -- computed
    # ONCE and used identically for both the returned JSON body and its own
    # hash, so the two can never silently diverge.
    canonical_body = to_canonical_payload(body)
    scorecard_artifact_hash = canonical_sha256(canonical_body)
    return {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "scorecard_payload": canonical_body,
        "scorecard_artifact_hash": scorecard_artifact_hash,
    }


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def render_markdown(envelope: Mapping[str, Any]) -> str:
    body = envelope["scorecard_payload"]
    lines: list[str] = []
    lines.append(f"# ROB-940 Historical Scorecard ({body['schema_version']})")
    lines.append("")
    lines.append(f"readiness: `{body['readiness']}`")
    lines.append("")
    lines.append(f"**campaign_verdict: {body['campaign_verdict']}**")
    lines.append(f"campaign_reason_codes: {json.dumps(body['campaign_reason_codes'])}")
    lines.append("")
    lineage = body["lineage"]
    lines.append("## Lineage")
    for key in (
        "full_campaign_hash",
        "campaign_run_id",
        "dataset_manifest_hash",
        "signal_manifest_hash",
        "trial_accounting_hash",
    ):
        lines.append(f"- {key}: `{lineage[key]}`")
    lines.append(f"- scorecard_artifact_hash: `{envelope['scorecard_artifact_hash']}`")
    lines.append("")

    for strategy, evidence in body["strategies"].items():
        lines.append(f"## {strategy}")
        verdict = evidence["verdict"]
        lines.append(
            f"**verdict: {verdict['verdict']}** (readiness: {verdict['readiness']})"
        )
        if verdict["reason_codes"]:
            lines.append(f"reason_codes: {', '.join(verdict['reason_codes'])}")
        lines.append("")
        lines.append(
            "| scenario | trades | expectancy_bps | PF | win_rate | net_pnl_bps | "
            "MDD_R | timeout_ratio | monthly_concentration | incomplete |"
        )
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for scenario_name in ("base", "primary_stress", "upward_stress"):
            s = evidence["scenarios"][scenario_name]
            lines.append(
                "| "
                + " | ".join(
                    _fmt(v)
                    for v in (
                        scenario_name,
                        s["trade_count"],
                        s["net_expectancy_bps"],
                        s["profit_factor"],
                        s["win_rate"],
                        s["net_pnl_bps"],
                        s["mdd_r"],
                        s["timeout_ratio"],
                        s["monthly_concentration"],
                        s["incomplete"],
                    )
                )
                + " |"
            )
        lines.append("")
        # I-4 final-fix: daily-stop counts and the reference-only ex-BTC
        # subtotal, per scenario -- JSON source of truth, Markdown only
        # renders it.
        for scenario_name in ("base", "primary_stress", "upward_stress"):
            s = evidence["scenarios"][scenario_name]
            subtotal = s["ex_btc_reference_subtotal"]
            lines.append(
                f"- {scenario_name}: daily_stop_active_count={s['daily_stop_active_count']} "
                f"no_trade_reason_counts={s['no_trade_reason_counts']} "
                f"ex_btc_reference_subtotal(reference_only={subtotal['reference_only']}, "
                f"has_pass_rule={subtotal['has_pass_rule']}): "
                f"trades={subtotal['trade_count']} signals={subtotal['signal_count']} "
                f"net_pnl_bps={_fmt(subtotal['net_pnl_bps'])} "
                f"pooled_expectancy_bps={_fmt(subtotal['pooled_expectancy_bps'])} "
                f"symbols={subtotal['symbols']}"
            )
        lines.append("")
        deltas = evidence["scenario_trade_count_deltas"]
        lines.append(
            "scenario_trade_count_deltas: "
            f"base_minus_primary_stress={deltas['base_minus_primary_stress']} "
            "primary_stress_minus_upward_stress="
            f"{deltas['primary_stress_minus_upward_stress']} "
            f"base_minus_upward_stress={deltas['base_minus_upward_stress']}"
        )
        if "spec_deviation_register" in evidence:
            register = evidence["spec_deviation_register"]
            lines.append(f"spec_deviation_register: {register['statement']}")
            lines.append(
                f"  total_rejection_counts: {register['total_rejection_counts']}"
            )
            for scenario_name, counts in register[
                "rejection_counts_by_scenario"
            ].items():
                lines.append(f"  {scenario_name}: {counts}")
        lines.append("")
        lines.append(
            f"positive OOS folds: {evidence['positive_oos_fold_count']} / "
            f"{len(evidence['fold_stability'])}"
        )
        concurrency = evidence["signal_concurrency"]
        lines.append(
            f"signal concurrency: numerator={concurrency['numerator']} "
            f"denominator={concurrency['denominator']} rate={_fmt(concurrency['rate'])} "
            f"reason={concurrency['reason']}"
        )
        pbo = evidence["pbo"]
        lines.append(
            f"PBO (auxiliary, reference-only): value={_fmt(pbo['value'])} "
            f"reason_codes={pbo['reason_codes']} slices={pbo['slices']}"
        )
        lines.append("")

    lines.append("## Symbol universe")
    lines.append("| symbol | historical_only | demo_execution_eligible | reason |")
    lines.append("|---|---|---|---|")
    for row in body["symbol_universe"]:
        lines.append(
            f"| {row['symbol']} | {row['historical_only']} | "
            f"{row['demo_execution_eligible']} | {_fmt(row.get('reason'))} |"
        )
    lines.append("")

    lines.append("## Disclosures")
    for key, value in body["disclosures"].items():
        lines.append(f"- **{key}**: {value}")

    return "\n".join(lines)
