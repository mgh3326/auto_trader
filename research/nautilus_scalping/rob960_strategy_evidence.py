"""ROB-960 -- assembles ONE strategy's build_scorecard `strategies[strategy]`
input entirely from ALREADY-COMPUTED evidence (a real WalkForwardResult, a
finalized capture sink, and a pre-computed cross-strategy concurrency
evidence). Wiring only: every compute_* call is an existing H5 pure
function, called exactly once, with no new metric definition.

Captain plan-gate G3: a PboGridError/Rob945PboBuilderError raised by
Task 1's compute_pbo_evidence_for_strategy is NEVER caught here -- it
propagates as a materialization abort. This module never fabricates or
degrades PBO evidence; `pbo_valid` is never set False by this materializer.
"""

from __future__ import annotations

from rob945_capture import CaptureInvalidError
from rob945_scenario_metrics import compute_fold_stability, compute_scenario_metrics
from rob960_pbo_evaluator import compute_pbo_evidence_for_strategy

_SCENARIOS = ("base", "primary_stress", "upward_stress")


def _fold_selected_config(walkforward_result) -> dict:
    return {
        fwr.fold.fold_id: fwr.selection_trace.selected_config_id
        for fwr in walkforward_result.folds
    }


def build_strategy_evidence(
    *,
    strategy,
    walkforward_result,
    capture_sink,
    signal_concurrency_evidence,
    bars_1m,
    funding_sidecars,
    gap_ranges,
):
    fold_selected_config = _fold_selected_config(walkforward_result)
    try:
        captured_signals = capture_sink.snapshot()
        capture_valid = True
    except CaptureInvalidError:
        captured_signals = ()
        capture_valid = False

    scenarios = {
        scenario_name: compute_scenario_metrics(
            strategy=strategy,
            scenario_name=scenario_name,
            ledger=walkforward_result.concatenated_oos_ledgers.get(scenario_name, ()),
            captured_signals=captured_signals,
            fold_selected_config=fold_selected_config,
        )
        for scenario_name in _SCENARIOS
    }

    fold_stability = compute_fold_stability(
        ledger=walkforward_result.concatenated_oos_ledgers.get("primary_stress", ()),
        fold_selected_config=fold_selected_config,
    )

    # No try/except: a PboGridError/Rob945PboBuilderError here is a genuine
    # materialization-abort condition (G3/G9), not a degraded evidence state
    # this function represents.
    pbo = compute_pbo_evidence_for_strategy(
        strategy=strategy,
        bars_1m=bars_1m,
        funding_sidecars=funding_sidecars,
        gap_ranges=gap_ranges,
    )

    return {
        "scenarios": scenarios,
        "fold_stability": fold_stability,
        "signal_concurrency": signal_concurrency_evidence,
        "pbo": pbo,
        "capture_valid": capture_valid,
    }
