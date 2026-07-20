"""ROB-983 (H5, CP7) -- pure JSON-only Markdown renderer.

``render_markdown`` accepts ONLY an already-``json.loads``-decoded canonical
scorecard (as produced by ``rob974_h5_canonical.build_canonical_scorecard`` +
``canonical_json_bytes``) -- never a raw H4/H6-A/DB object, and it never
recomputes a metric. It is presentation only: every reason/attribution/
verdict value is read verbatim from the canonical tree and formatted.

Every dict-shaped substructure (``status_counts``, attribution buckets) is
walked in the SAME registered domain order CP6 already canonicalized with
(rather than trusting the input mapping's own iteration order), so a
hypothetical differently-ordered-but-semantically-identical input dict still
produces byte-identical Markdown. List-shaped fields (``dual_evidence``,
``reasons``, ``incomplete_reasons``, ``expected_experiment_ids``) are
positionally meaningful JSON arrays already in canonical order from CP6 and
are rendered in the given order.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rob974_h5_canonical import CLOSED_STATUS_ORDER
from rob974_h5_contracts import (
    S3_EXIT_REASONS,
    S3_SYMBOLS,
    S4_EXIT_REASONS,
    S4_PAIRS,
    STRATEGIES,
)

__all__ = ["render_markdown"]

_DIMENSION_ORDER = {"S3": S3_SYMBOLS, "S4": S4_PAIRS}
_DIMENSION_KEY = {"S3": "by_symbol", "S4": "by_pair"}
_EXIT_REASON_ORDER = {"S3": S3_EXIT_REASONS, "S4": S4_EXIT_REASONS}


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _fmt_list(values: Sequence[str]) -> str:
    return ", ".join(values) if values else "(none)"


def _render_bucket_line(label: str, bucket: Mapping[str, Any]) -> str:
    pf = _fmt(bucket["pf"])
    if bucket.get("pf_reason"):
        pf = f"{pf} ({bucket['pf_reason']})"
    return (
        f"- {label}: trades={bucket['trades']} e17_bps={_fmt(bucket['e17_bps'])} "
        f"e0_bps={_fmt(bucket['e0_bps'])} pf={pf} "
        f"avg_holding_minutes={_fmt(bucket['avg_holding_minutes'])}"
    )


def _render_ordered_buckets(
    by_key: Mapping[str, Mapping[str, Any]], order: Sequence[str]
) -> list[str]:
    return [_render_bucket_line(key, by_key[key]) for key in order if key in by_key]


def _render_dual_evidence(rows: Sequence[Mapping[str, Any]]) -> list[str]:
    if not rows:
        return []
    lines = ["### Dual Evidence", ""]
    for row in rows:
        lines.append(
            f"- {row['config_id']}/{row['fold_id']}: "
            f"accepted={row['accepted']} rejected={row['rejected']}"
        )
        histogram = row["rejection_reason_histogram"]
        if histogram:
            # Sorted explicitly -- never trust the input mapping's own
            # iteration order (mirrors CP6's own histogram canonicalization).
            reasons_str = ", ".join(f"{k}={histogram[k]}" for k in sorted(histogram))
            lines.append(f"  - rejection_reasons: {reasons_str}")
        for path in row["paths"]:
            lines.append(
                f"  - path[{path['path_scenario']}]: "
                f"ledger_status={path['ledger_status']} "
                f"trade_count={path['trade_count']}"
            )
    lines.append("")
    return lines


def _render_pbo(pbo: Mapping[str, Any] | None) -> list[str]:
    if pbo is None:
        return []
    lines = ["### PBO", ""]
    lines.append(f"- value: {_fmt(pbo['value'])}")
    lines.append(f"- reason_codes: {_fmt_list(pbo['reason_codes'])}")
    lines.append("")
    return lines


def _render_pair_executor_state(state: Mapping[str, Any] | None) -> list[str]:
    if state is None:
        return []
    lines = ["### Pair Executor State (historical)", ""]
    lines.append(f"- pair_executor_state: {state['pair_executor_state']}")
    lines.append(f"- readiness: {state['readiness']}")
    lines.append(f"- demo_eligible: {_fmt(state['demo_eligible'])}")
    lines.append("")
    return lines


def _render_strategy(strategy: str, entry: Mapping[str, Any]) -> list[str]:
    lines = [f"## Strategy {strategy}", ""]

    gates = entry["common_gates"]
    lines.append("### Common Gates")
    lines.append(f"- passed: {_fmt(gates['passed'])}")
    lines.append(f"- reasons: {_fmt_list(gates['reasons'])}")
    lines.append(f"- pooled_e17_bps: {_fmt(gates['pooled_e17_bps'])}")
    pf17 = _fmt(gates["pf17"])
    if gates.get("pf17_reason"):
        pf17 = f"{pf17} ({gates['pf17_reason']})"
    lines.append(f"- pf17: {pf17}")
    lines.append(f"- win_margin: {_fmt(gates['win_margin'])}")
    lines.append(f"- monthly_concentration: {_fmt(gates['monthly_concentration'])}")
    lines.append("")

    falsification = entry["falsification"]
    lines.append("### Falsification")
    lines.append(f"- reasons: {_fmt_list(falsification['reasons'])}")
    lines.append(
        f"- incomplete_reasons: {_fmt_list(falsification['incomplete_reasons'])}"
    )
    lines.append("")

    lines.append("### Attribution: by_exit_reason")
    lines.extend(
        _render_ordered_buckets(
            falsification["attribution"]["by_exit_reason"], _EXIT_REASON_ORDER[strategy]
        )
    )
    lines.append("")

    dimension_key = _DIMENSION_KEY[strategy]
    lines.append(f"### Attribution: {dimension_key}")
    lines.extend(
        _render_ordered_buckets(
            falsification["attribution"][dimension_key], _DIMENSION_ORDER[strategy]
        )
    )
    lines.append("")

    lines.extend(_render_dual_evidence(entry["dual_evidence"]))
    lines.extend(_render_pbo(entry["pbo"]))
    lines.extend(_render_pair_executor_state(entry.get("pair_executor_state")))

    lines.append(f"### Direct Verdict: {entry['direct_verdict']}")
    lines.append("")
    return lines


def render_markdown(canonical: Mapping[str, Any]) -> bytes:
    lines: list[str] = [f"# H5 Scorecard ({canonical['schema_version']})", ""]

    lineage = canonical["lineage"]
    lines.append("## Lineage")
    lines.append(f"- campaign_run_id: {lineage['campaign_run_id']}")
    lines.append(f"- full_campaign_hash: {lineage['full_campaign_hash']}")
    lines.append(f"- run_schema_version: {lineage['run_schema_version']}")
    lines.append(f"- generator_version: {lineage['generator_version']}")
    lines.append(f"- actual_h4_ledger_key: {lineage['actual_h4_ledger_key']}")
    lines.append("")

    h6a = canonical["h6a_accounting"]
    lines.append("## H6-A Accounting")
    lines.append(f"- expected_total: {h6a['expected_total']}")
    lines.append(f"- registered_total: {h6a['registered_total']}")
    lines.append(f"- accounting_complete: {_fmt(h6a['accounting_complete'])}")
    lines.append(f"- performance_usable: {_fmt(h6a['performance_usable'])}")
    for status in CLOSED_STATUS_ORDER:
        if status in h6a["status_counts"]:
            lines.append(f"  - status[{status}]: {h6a['status_counts'][status]}")
    lines.append(f"- reason_codes: {_fmt_list(h6a['reason_codes'])}")
    lines.append("")

    env_val = canonical["envelope_validation"]
    lines.append("## Envelope Validation")
    lines.append(f"- ok: {_fmt(env_val['ok'])}")
    lines.append(f"- incomplete_reasons: {_fmt_list(env_val['incomplete_reasons'])}")
    lines.append("")

    for strategy in STRATEGIES:
        lines.extend(_render_strategy(strategy, canonical["strategies"][strategy]))

    campaign = canonical["campaign_decision"]
    lines.append("## Campaign Decision")
    lines.append(f"- campaign_decision: {campaign['campaign_decision']}")
    lines.append(
        f"- campaign_historical_verdict: {campaign['campaign_historical_verdict']}"
    )
    lines.append(f"- s3_direct_verdict: {campaign['s3_direct_verdict']}")
    lines.append(f"- s4_direct_verdict: {campaign['s4_direct_verdict']}")
    lines.append(f"- demo_candidate: {_fmt(campaign['demo_candidate'])}")
    lines.append(f"- historical_preferred: {_fmt(campaign['historical_preferred'])}")
    lines.append(
        f"- s4_observable_superiority: {_fmt(campaign['s4_observable_superiority'])}"
    )
    lines.append("")

    return ("\n".join(lines) + "\n").encode("utf-8")
