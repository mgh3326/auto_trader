"""ROB-383 Phase 3 - map validated_gate reports to sieve classes."""

from __future__ import annotations

from validated_gate import GateReport


def _folds(report: GateReport) -> dict[str, dict]:
    return {fold.get("fold", ""): fold for fold in report.per_fold}


def classify(
    report: GateReport,
    *,
    notional: float = 1000.0,
    economic_floor_bps: float = 0.5,
) -> tuple[str, list[str]]:
    if report.verdict == "insufficient_data":
        return "research_candidate", [
            "underpowered: " + "; ".join(report.verdict_reasons)
        ]

    gross = report.results.get("gross", {}).get("net_pnl", 0.0)
    net = report.results.get("net_after_cost", {}).get("net_pnl", 0.0)

    if report.verdict == "not_validated":
        if gross <= 0 or net <= 0:
            return "reject", [
                f"gross={gross:.2f}, net@fee={net:.2f}; "
                + "; ".join(report.verdict_reasons)
            ]
        return "research_candidate", [
            "gross-positive but failed gate: " + "; ".join(report.verdict_reasons)
        ]

    folds = _folds(report)
    oos = folds.get("oos", {})
    oos_bps = (oos.get("expectancy", 0.0) / notional) * 1e4 if notional else 0.0
    all_folds_pos = all(
        folds.get(fold, {}).get("net_pnl", 0.0) > 0 for fold in ("train", "val", "oos")
    )
    if all_folds_pos and oos_bps >= economic_floor_bps:
        return "demo_ready_candidate", [
            f"validated; oos {oos_bps:.2f} bps/trade >= floor "
            f"{economic_floor_bps}; positive across all folds. Small Demo "
            "observation may be justified with SEPARATE operator approval."
        ]
    return "shadow_candidate", [
        f"validated on oos at demo taker (oos {oos_bps:.2f} bps/trade); "
        "signal-only / dry-run observation candidate."
    ]
