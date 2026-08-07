"""Comparator and terminal-state resolution for the B0 calibration.

Every predicate is a frozen literal from contract v3.1 §5 as closed by
``d3-calibration-gap-closure-20260807.md``:

* positive scale — ``0.5 <= simulated / actual <= 2.0``
* bounded share — ``abs(simulated - actual) <= 0.20``
* signed percentage points — ``abs(simulated - actual) <= 5.0``
* exact-zero rule — **positive-scale only** (GAP-10 restores the contract
  wording over the preparation packet's conservative extension)
* an empty or invalid sample is ``NOT_COMPUTABLE`` and never coerced to 0,
  PASS, or MISMATCH (GAP-06)

None of these states selects a D3 winner or re-ranks anything economic.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from research.kr_corpus.d3_engine.calibration_metrics import (
    COMPARISON_KIND,
    METRIC_IDS,
    contract_context,
    decimal_text,
)

STATE_PRIORITY_A1 = (
    "RUN_INVALID",
    "INCONCLUSIVE_DATA_BIAS",
    "INCONCLUSIVE_UNRESOLVED_TERMINAL",
    "CALIBRATION_DATA_BIAS",
    "CALIBRATION_MISMATCH",
    "verdict",
)
POSITIVE_SCALE_LOW = Decimal("0.5")
POSITIVE_SCALE_HIGH = Decimal("2.0")
BOUNDED_SHARE_TOLERANCE = Decimal("0.20")
SIGNED_POINT_TOLERANCE = Decimal("5.0")

TERMINAL_ORDER = (
    "RUN_INVALID",
    "INCONCLUSIVE_UNRESOLVED_TERMINAL",
    "CALIBRATION_INCOMPLETE",
    "CALIBRATION_DATA_BIAS",
    "CALIBRATION_MISMATCH",
    "CALIBRATION_PASS",
)


def _aggregate(side: dict[str, Any] | None) -> Decimal | None:
    if not side:
        return None
    if int(side.get("n", 0)) <= 0:
        return None
    raw = side.get("aggregate_decimal")
    if raw is None:
        return None
    value = Decimal(str(raw))
    if not value.is_finite():
        return None
    return value


def compare_view(
    metric_id: str,
    actual: dict[str, Any] | None,
    simulated: dict[str, Any] | None,
) -> tuple[str, dict[str, Any]]:
    """One view's decision plus the exact numbers the decision used."""

    kind = COMPARISON_KIND[metric_id]
    actual_value = _aggregate(actual)
    simulated_value = _aggregate(simulated)
    detail: dict[str, Any] = {
        "comparison_kind": kind,
        "actual_aggregate": (
            decimal_text(actual_value) if actual_value is not None else None
        ),
        "simulated_aggregate": (
            decimal_text(simulated_value) if simulated_value is not None else None
        ),
        "zero_rule_applied": False,
    }
    if actual_value is None or simulated_value is None:
        detail["reason"] = "empty_or_invalid_sample_on_at_least_one_side"
        return "NOT_COMPUTABLE", detail

    if kind == "positive_scale" and actual_value == 0:
        detail["zero_rule_applied"] = True
        detail["predicate"] = "contract exact-zero rule: simulated must be exactly 0"
        return ("PASS" if simulated_value == 0 else "FAIL"), detail

    if kind == "positive_scale":
        with contract_context():
            ratio = simulated_value / actual_value
        detail["ratio"] = decimal_text(ratio)
        detail["predicate"] = "0.5 <= simulated/actual <= 2.0"
        passed = POSITIVE_SCALE_LOW <= ratio <= POSITIVE_SCALE_HIGH
    elif kind == "bounded_share":
        with contract_context():
            difference = abs(simulated_value - actual_value)
        detail["absolute_difference"] = decimal_text(difference)
        detail["predicate"] = "abs(simulated - actual) <= 0.20"
        passed = difference <= BOUNDED_SHARE_TOLERANCE
    else:
        with contract_context():
            difference = abs(simulated_value - actual_value)
        detail["absolute_difference_points"] = decimal_text(difference)
        detail["predicate"] = "abs(simulated - actual) <= 5.0 percentage points"
        passed = difference <= SIGNED_POINT_TOLERANCE
    return ("PASS" if passed else "FAIL"), detail


def view_outcome(original: str, clamp: str) -> str:
    if "NOT_COMPUTABLE" in (original, clamp):
        return "NOT_COMPUTABLE"
    if original == "PASS" and clamp == "PASS":
        return "PASS"
    if original == "FAIL" and clamp == "FAIL":
        return "CALIBRATION_MISMATCH"
    return "CALIBRATION_DATA_BIAS"


def build_result(
    *,
    actual_metrics: dict[str, Any],
    simulated_metrics: dict[str, dict[str, Any]],
    engine_statuses: dict[str, str],
    stamps: dict[str, Any],
    census: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the per-metric matrix and resolve the terminal state."""

    rows: list[dict[str, Any]] = []
    for metric_id in METRIC_IDS:
        actual = actual_metrics.get(metric_id)
        original = simulated_metrics["original"].get(metric_id)
        clamp = simulated_metrics["clamp"].get(metric_id)
        original_decision, original_detail = compare_view(metric_id, actual, original)
        clamp_decision, clamp_detail = compare_view(metric_id, actual, clamp)
        outcome = view_outcome(original_decision, clamp_decision)
        rows.append(
            {
                "metric_id": metric_id,
                "comparison_kind": COMPARISON_KIND[metric_id],
                "actual": actual,
                "simulated": {"original": original, "clamp": clamp},
                "zero_rule_applied": bool(original_detail["zero_rule_applied"]),
                "original_decision": original_decision,
                "clamp_decision": clamp_decision,
                "view_outcome": outcome,
                "original_comparison": original_detail,
                "clamp_comparison": clamp_detail,
                "not_computable_reason": (
                    original_detail.get("reason") or clamp_detail.get("reason")
                    if outcome == "NOT_COMPUTABLE"
                    else None
                ),
            }
        )

    unresolved = sorted(
        run_id for run_id, status in engine_statuses.items() if status != "OK"
    )
    not_computable = [
        row["metric_id"] for row in rows if row["view_outcome"] == "NOT_COMPUTABLE"
    ]
    data_bias = [
        row["metric_id"]
        for row in rows
        if row["view_outcome"] == "CALIBRATION_DATA_BIAS"
    ]
    mismatch = [
        row["metric_id"]
        for row in rows
        if row["view_outcome"] == "CALIBRATION_MISMATCH"
    ]

    if unresolved:
        terminal = "INCONCLUSIVE_UNRESOLVED_TERMINAL"
        basis = f"engine status is not OK for {unresolved}"
        routed = "NEEDS_UPSTREAM(unresolved_terminal_in_calibration_replay)"
    elif not_computable:
        terminal = "CALIBRATION_INCOMPLETE"
        basis = (
            "GAP-06: a required metric is NOT_COMPUTABLE and is neither read as "
            f"0 nor promoted to a mismatch — {not_computable}"
        )
        routed = "NEEDS_UPSTREAM(empty_or_invalid_metric_input)"
    elif data_bias:
        terminal = "CALIBRATION_DATA_BIAS"
        basis = f"the two views disagree on {data_bias}; a mismatch cannot be judged"
        routed = "D3.1 design input — view disagreement"
    elif mismatch:
        terminal = "CALIBRATION_MISMATCH"
        basis = (
            f"both views fail the frozen acceptance band on {mismatch}; the "
            "convention is not revised inside this run"
        )
        routed = "D3.1 branch"
    else:
        terminal = "CALIBRATION_PASS"
        basis = "every metric passes the frozen band on both views"
        routed = "D3.1 model-admission input"

    per_view = {
        view: {
            "pass": sorted(
                row["metric_id"] for row in rows if row[f"{view}_decision"] == "PASS"
            ),
            "fail": sorted(
                row["metric_id"] for row in rows if row[f"{view}_decision"] == "FAIL"
            ),
            "not_computable": sorted(
                row["metric_id"]
                for row in rows
                if row[f"{view}_decision"] == "NOT_COMPUTABLE"
            ),
        }
        for view in ("original", "clamp")
    }

    return {
        "schema_id": "d3.calibration.result.v2",
        "label": "CALIBRATION_DIAGNOSTIC_ONLY",
        "purpose": "D3.1_MODEL_INPUT_ONLY",
        "cell": "B0 x with_contribution x {original, clamp} = 2 physical",
        "state_priority_a1": STATE_PRIORITY_A1,
        "terminal_state_evaluation_order": TERMINAL_ORDER,
        "top_level_status": terminal,
        "terminal_state_basis": basis,
        "routed_to": routed,
        "dual_view": per_view,
        "winner_touched": False,
        "economic_reranking": False,
        "inconclusive_data_bias_released": False,
        "stamps": stamps,
        "census": census,
        "metrics": rows,
    }
