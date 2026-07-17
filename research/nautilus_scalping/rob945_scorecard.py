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

import base64
import re
from collections.abc import Mapping
from typing import Any

import rob941_frozen_scope as frozen
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
ACCOUNTING_DRIFT_REASON = "h6_accounting_incomplete"
RUN_ID_DRIFT_REASON = "campaign_run_id_derivation_mismatch"
HASH_FORMAT_REASON = "hash_field_not_lowercase_64_hex"
FOLD_ID_SEQUENCE_REASON = "fold_id_sequence_not_canonical_contiguous"
ACCOUNTING_ATTEMPTS_REASON = "h6_accounting_attempts_not_sealed_24"

_LOWERCASE_HEX_64 = re.compile(r"^[0-9a-f]{64}$")
EXPECTED_PRIMARY_ATTEMPT_COUNT = 24


def _derive_campaign_run_id(full_campaign_hash: str) -> str:
    """Bit-for-bit the SAME recipe as
    ``run_rob944_campaign._derive_primary_campaign_run_id`` /
    ``rob944_campaign_controller._derive_expected_campaign_run_id``: SHA-256
    of ``{"full_campaign_hash": ..., "kind": "primary_run"}`` -> raw 32
    bytes -> unpadded URL-safe base64 (43 chars) -> ``"rob944-primary-"``
    prefix (15 chars) -> 58 chars total. Re-derived here (never trusted
    from the caller) as the third independent cross-check this lineage
    already requires."""
    digest_hex = canonical_sha256(
        {"full_campaign_hash": full_campaign_hash, "kind": "primary_run"}
    )
    raw = bytes.fromhex(digest_hex)
    suffix = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"rob944-primary-{suffix}"


def _validate_attempt_evidence(attempt_evidence: Any) -> None:
    """``attempt_evidence`` is a SEPARATE sealed tuple of exactly 24
    canonical primary ``AttemptEvidence``-shaped records -- the canonical
    ``CampaignCompletenessReport`` DTO itself has no ``attempts`` field, so
    this is never invented as a key inside ``accounting_report``; it is its
    own explicit input, cross-hashed alongside the report."""
    if (
        not isinstance(attempt_evidence, list | tuple)
        or len(attempt_evidence) != EXPECTED_PRIMARY_ATTEMPT_COUNT
    ):
        raise ScorecardInputError(ACCOUNTING_ATTEMPTS_REASON)
    seen_experiment_ids: set[str] = set()
    for attempt in attempt_evidence:
        if not isinstance(attempt, Mapping):
            raise ScorecardInputError(ACCOUNTING_ATTEMPTS_REASON)
        experiment_id = attempt.get("experiment_id")
        retry_index = attempt.get("retry_index")
        status = attempt.get("status")
        if (
            not isinstance(experiment_id, str)
            or not experiment_id
            or retry_index != 0
            or not isinstance(status, str)
            or not status
        ):
            raise ScorecardInputError(ACCOUNTING_ATTEMPTS_REASON)
        if experiment_id in seen_experiment_ids:
            raise ScorecardInputError(ACCOUNTING_ATTEMPTS_REASON)
        seen_experiment_ids.add(experiment_id)
    if len(seen_experiment_ids) != EXPECTED_PRIMARY_ATTEMPT_COUNT:
        raise ScorecardInputError(ACCOUNTING_ATTEMPTS_REASON)


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


class ScorecardInputError(ValueError):
    """The sealed H5 evidence input failed a fail-closed boundary check --
    mismatched identity, hash drift, incomplete accounting, missing fold/
    symbol/scenario coverage, or partial scenario evidence."""


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise ScorecardInputError(reason)


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
    for scenario_name, aggregate in scenarios.items():
        _require(
            aggregate.scenario_name == scenario_name,
            f"scenario_name_mismatch_for_{strategy}_{scenario_name}",
        )
        symbol_set = {m.symbol for m in aggregate.symbol_metrics}
        _require(
            symbol_set == set(_REQUIRED_SYMBOLS),
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

    return {
        "strategy": strategy,
        "scenarios": {
            name: _scenario_aggregate_payload(agg) for name, agg in scenarios.items()
        },
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
    strategies: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    for hash_value in (full_campaign_hash, dataset_manifest_hash, signal_manifest_hash):
        _require(bool(_LOWERCASE_HEX_64.match(hash_value)), HASH_FORMAT_REASON)

    recomputed_hash = canonical_sha256(to_canonical_payload(full_campaign_payload))
    _require(recomputed_hash == full_campaign_hash, HASH_DRIFT_REASON)

    expected_campaign_run_id = _derive_campaign_run_id(full_campaign_hash)
    _require(campaign_run_id == expected_campaign_run_id, RUN_ID_DRIFT_REASON)

    accounting_complete = accounting_report.get("verdict") == "complete"
    _require(accounting_complete, ACCOUNTING_DRIFT_REASON)
    _validate_attempt_evidence(attempt_evidence)

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
    if any(v == "incomplete" for v in strategy_verdicts):
        campaign_verdict = "incomplete"
    elif any(v == "historical_fail" for v in strategy_verdicts):
        campaign_verdict = "historical_fail"
    else:
        campaign_verdict = "historical_pass"

    overall_numerator = sum(
        strategy_payloads[s]["signal_concurrency"]["numerator"] or 0
        for s in _REQUIRED_STRATEGIES
    )
    overall_denominator = sum(
        strategy_payloads[s]["signal_concurrency"]["denominator"] or 0
        for s in _REQUIRED_STRATEGIES
    )

    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generator_version": GENERATOR_VERSION,
        "readiness": READINESS,
        "campaign_verdict": campaign_verdict,
        "lineage": {
            "full_campaign_hash": full_campaign_hash,
            "campaign_run_id": campaign_run_id,
            "dataset_manifest_hash": dataset_manifest_hash,
            "signal_manifest_hash": signal_manifest_hash,
            # hashes the FULL sealed evidence (report + all 24 attempt
            # records), never just a bare {"verdict": "complete"} string.
            "trial_accounting_hash": canonical_sha256(
                to_canonical_payload(
                    {
                        "report": dict(accounting_report),
                        "attempts": [dict(a) for a in attempt_evidence],
                    }
                )
            ),
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
