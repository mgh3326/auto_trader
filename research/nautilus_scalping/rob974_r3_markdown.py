"""Pure deterministic Markdown renderer for the ROB-974 R3 scorecard."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from rob974_r3_scorecard import (
    R3_SCORECARD_SCHEMA_VERSION,
    canonical_r3_json_bytes,
    hash_r3_canonical_bytes,
)

_SEMANTIC_LINE = re.compile(rb"^<!-- rob974-r3-semantic-sha256:([0-9a-f]{64}) -->$")


def _fmt(value: Any) -> str:
    if value is None:
        return "null"
    if type(value) is bool:
        return "true" if value else "false"
    return str(value)


def _metric_fmt(value: object) -> str:
    assert isinstance(value, Mapping)
    observed = value["value"]
    if observed is not None:
        return _fmt(observed)
    return f"null({value['reason']})"


def _fold_counts(rows: object, field: str) -> str:
    assert isinstance(rows, list)
    return ",".join(
        f"{row['fold_id']}:{_fmt(row[field])}"
        for row in rows
        if isinstance(row, Mapping)
    )


def _attribution_summary(value: object) -> str:
    assert isinstance(value, Mapping)
    buckets = (
        value["by_exit_reason"] if "by_exit_reason" in value else value["by_dimension"]
    )
    assert isinstance(buckets, Mapping)
    observed = ",".join(
        f"{name}:{bucket['trades']}/{_metric_fmt(bucket['e17_bps'])}"
        for name, bucket in buckets.items()
        if isinstance(bucket, Mapping)
    )
    reason = value["reason"]
    suffix = "" if reason is None else f";reason={reason}"
    return f"{value['status']}[{observed}]{suffix}"


def _compact(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).replace("|", "\\u007c")


def render_r3_markdown(
    canonical: Mapping[str, object], *, semantic_sha256: str
) -> bytes:
    """Render only already-canonical semantics and bind their SHA-256."""

    if type(semantic_sha256) is not str or not re.fullmatch(
        r"[0-9a-f]{64}", semantic_sha256
    ):
        raise ValueError("semantic_sha256 must be lowercase 64-hex")
    observed = hash_r3_canonical_bytes(canonical_r3_json_bytes(canonical))
    if observed != semantic_sha256:
        raise ValueError("Markdown semantic hash differs from canonical JSON")
    verdict = canonical["campaign_verdict"]
    operational = canonical["operational"]
    lineage = canonical["lineage"]
    cells = canonical["cells"]
    section3 = canonical["section3_falsification"]
    section5 = canonical["section5_gate_audit"]
    section7 = canonical["section7_relaxation"]
    family_verdicts = canonical["family_verdicts"]
    assert isinstance(verdict, Mapping)
    assert isinstance(operational, Mapping)
    assert isinstance(lineage, Mapping)
    assert isinstance(cells, list)
    assert isinstance(section3, list)
    assert isinstance(section5, Mapping)
    assert isinstance(section7, Mapping)
    assert isinstance(family_verdicts, list)
    lines = [
        f"<!-- rob974-r3-semantic-sha256:{semantic_sha256} -->",
        f"# ROB-974 R3 H5 Scorecard ({R3_SCORECARD_SCHEMA_VERSION})",
        "",
        "## Lineage",
        "",
        f"- campaign_identity_sha256: {lineage['campaign_identity_sha256']}",
        f"- campaign_run_id: {lineage['campaign_run_id']}",
        f"- exact_12_mapping_hash: {lineage['exact_12_mapping_hash']}",
        "",
        "## Operational Status",
        "",
        f"- status: {operational['status']}",
        "- incomplete_reasons: "
        + (
            ", ".join(operational["incomplete_reasons"])
            if operational["incomplete_reasons"]
            else "(none)"
        ),
        "",
        "## §3 Frozen Falsification Table",
        "",
        "| claim_id | status | observed | reason |",
        "|---|---|---|---|",
    ]
    for row in section3:
        assert isinstance(row, Mapping)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(row["claim_id"]),
                    str(row["status"]),
                    _fmt(row["observed"]),
                    str(row["reason"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## Exact-12 Cells",
            "",
            "| config | accepted | trades | E0 | E13 | E17 | E22_up | PF17 | win-margin |",
            "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
        )
    )
    for cell in cells:
        assert isinstance(cell, Mapping)
        metrics = cell["economics"]
        assert isinstance(metrics, Mapping)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(cell["config_id"]),
                    _fmt(cell["accepted_total"]),
                    _fmt(cell["basket_trades_total"]),
                    _metric_fmt(metrics["e0_bps"]),
                    _metric_fmt(metrics["e13_bps"]),
                    _metric_fmt(metrics["e17_bps"]),
                    _metric_fmt(metrics["e22_up_bps"]),
                    _metric_fmt(metrics["pf17"]),
                    _metric_fmt(metrics["win_margin_at_17"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## Preregistered Per-Cell Diagnostics",
            "",
            "Each attribution bucket is rendered as `trades/E17`; null values retain their closed reason.",
            "",
            "| config | operational | research eligible | accepted by fold | trades by fold | positive folds | month concentration | conversion | observed win | weighted pBE | PBO | strategy gates | §5 | §7 |",
            "|---|---|---|---|---|---:|---:|---:|---:|---:|---|---|---|---|",
        )
    )
    for cell in cells:
        assert isinstance(cell, Mapping)
        pbo = cell["pbo"]
        assert isinstance(pbo, Mapping)
        strategy = cell["strategy_gates"]
        gate_summary = cell["section5_gate_audit_summary"]
        relaxation_reference = cell["section7_relaxation_reference"]
        assert isinstance(strategy, Mapping)
        assert isinstance(gate_summary, Mapping)
        assert isinstance(relaxation_reference, Mapping)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(cell["config_id"]),
                    str(cell["operational_status"]),
                    _fmt(cell["research_eligible"]),
                    _fold_counts(cell["accepted_by_fold"], "accepted"),
                    _fold_counts(cell["basket_trades_by_fold"], "basket_trades"),
                    _fmt(cell["positive_oos_folds"]),
                    _metric_fmt(cell["monthly_concentration"]),
                    _metric_fmt(cell["accepted_to_trade_conversion"]),
                    _metric_fmt(cell["observed_win_rate"]),
                    _metric_fmt(cell["weighted_p_be"]),
                    f"{pbo['status']}:{_fmt(pbo['value'])}({pbo['reason']})",
                    str(strategy["status"]),
                    str(gate_summary["status"]),
                    str(relaxation_reference["status"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## Exit/Timeout and Symbol/Pair Attribution",
            "",
            "| config | exit/timeout | symbol/pair |",
            "|---|---|---|",
        )
    )
    for cell in cells:
        assert isinstance(cell, Mapping)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(cell["config_id"]),
                    _attribution_summary(cell["exit_and_timeout_attribution"]),
                    _attribution_summary(cell["symbol_or_pair_attribution"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## §5 Gate Audit",
            "",
            f"- status: {section5['status']}",
            f"- schema_version: {_fmt(section5['schema_version'])}",
            f"- report_count: {len(section5['report_order'])}",
            f"- evidence_cell_count: {len(section5['evidence_cell_order'])}",
            "",
            "## §7 Relaxation",
            "",
            f"- status: {section7['status']}",
            f"- schema_version: {_fmt(section7['schema_version'])}",
            f"- ray_order: {_compact(section7['ray_order'])}",
            f"- oos_present: {_fmt(section7['oos'] is not None)}",
            f"- train_diagnostic_present: {_fmt(section7['train_diagnostic'] is not None)}",
            "",
            "## Family Verdicts",
            "",
            "| family | operational | research decision | full winners | qualifying adjacency | reasons |",
            "|---|---|---|---|---|---|",
        )
    )
    for family in family_verdicts:
        assert isinstance(family, Mapping)
        lines.append(
            "| "
            + " | ".join(
                (
                    str(family["family"]),
                    str(family["operational_status"]),
                    _fmt(family["research_decision"]),
                    _compact(family["full_gate_winners"]),
                    _compact(family["qualifying_adjacent_pairs"]),
                    _compact(family["reason_codes"]),
                )
            )
            + " |"
        )
    lines.extend(
        (
            "",
            "## §8 Campaign Verdict",
            "",
            f"- operational_status: {verdict['operational_status']}",
            f"- research_decision: {_fmt(verdict['research_decision'])}",
            "- reason_codes: "
            + (
                ", ".join(verdict["reason_codes"])
                if verdict["reason_codes"]
                else "(none)"
            ),
            "",
        )
    )
    return "\n".join(lines).encode("utf-8")


def verify_r3_markdown_semantic_binding(
    *, canonical: Mapping[str, object], markdown_bytes: bytes
) -> str:
    if type(markdown_bytes) is not bytes:
        raise TypeError("markdown_bytes must be exact bytes")
    first_line = markdown_bytes.splitlines()[0] if markdown_bytes else b""
    match = _SEMANTIC_LINE.fullmatch(first_line)
    if match is None:
        raise ValueError("Markdown semantic binding is missing or malformed")
    semantic_sha256 = match.group(1).decode("ascii")
    if markdown_bytes != render_r3_markdown(canonical, semantic_sha256=semantic_sha256):
        raise ValueError("Markdown semantic mismatch")
    return semantic_sha256


__all__ = ["render_r3_markdown", "verify_r3_markdown_semantic_binding"]
