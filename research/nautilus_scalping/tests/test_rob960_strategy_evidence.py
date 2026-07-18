"""ROB-960 -- per-strategy build_scorecard evidence assembler tests.

Wiring only: every compute_* call is an existing H5 pure function. Per
captain plan-gate G3, a PboGridError/Rob945PboBuilderError must NEVER be
caught here -- it must propagate as a materialization abort.
"""

from __future__ import annotations

import rob941_frozen_scope as frozen
from rob940_bars_agg import Bar1m
from rob941_funding_sidecar import FundingSidecar
from rob944_folds import Fold
from rob944_selection import ConfigSelectionOutcome, FoldSelectionTrace
from rob944_walkforward import (
    ConfigAttemptResult,
    FoldWalkForwardResult,
    WalkForwardResult,
)
from rob945_capture import OosSignalCaptureSink
from rob945_signal_concurrency import StrategyConcurrencyEvidence
from rob960_strategy_evidence import build_strategy_evidence


def _flat_bars_1m():
    bars = tuple(
        Bar1m(
            ts=frozen.WINDOW_START_MS + i * 60_000,
            open=100.0,
            high=100.0,
            low=100.0,
            close=100.0,
            volume=1.0,
        )
        for i in range(5)
    )
    return dict.fromkeys(frozen.UNIVERSE, bars)


def _flat_funding_sidecars():
    return {s: FundingSidecar.from_rows(s, ()) for s in frozen.UNIVERSE}


def _empty_gap_ranges():
    return dict.fromkeys(frozen.UNIVERSE, ())


def _empty_selection_outcome(config_id: str) -> ConfigSelectionOutcome:
    return ConfigSelectionOutcome(
        config_id=config_id,
        eligible_symbols=(),
        excluded_symbols=(),
        equal_weight_expectancy_bps=None,
        pooled_expectancy_bps=None,
        profit_factor=0.0,
        rejected=True,
        rejection_reason="insufficient_eligible_symbols",
        train_input_hash="0" * 64,
        no_trade_reason_counts={},
    )


def _walkforward_result_no_selection(strategy: str) -> WalkForwardResult:
    config_ids = tuple(f"{strategy}-{i:02d}" for i in range(12))
    fold_results = []
    for i in range(8):
        base = i * 100
        fold = Fold(
            fold_id=f"fold-{i:02d}",
            fold_index=i,
            train_start_ms=base,
            train_end_ms=base + 10,
            embargo_start_ms=base + 10,
            embargo_end_ms=base + 20,
            oos_start_ms=base + 20,
            oos_end_ms=base + 30,
        )
        candidates = tuple(_empty_selection_outcome(cid) for cid in config_ids)
        trace = FoldSelectionTrace(
            strategy=strategy, candidates=candidates, selected_config_id=None
        )
        fold_results.append(
            FoldWalkForwardResult(fold=fold, selection_trace=trace, oos_outcomes=())
        )
    attempts = tuple(
        ConfigAttemptResult(
            strategy=strategy,
            config_id=cid,
            status="completed",
            reason_code=None,
            selected_in_folds=(),
            crash_log=(),
            gap_rejection_log=(),
        )
        for cid in config_ids
    )
    return WalkForwardResult(
        strategy=strategy,
        folds=tuple(fold_results),
        config_attempts=attempts,
        concatenated_oos_ledgers={},
    )


def _concurrency(strategy: str) -> StrategyConcurrencyEvidence:
    return StrategyConcurrencyEvidence(
        strategy=strategy,
        numerator=0,
        denominator=0,
        rate=None,
        reason="no_entry_signal_minutes",
        distinct_symbol_count_histogram={1: 0, 2: 0, 3: 0, 4: 0},
    )


def test_invalid_capture_sink_marks_capture_valid_false_without_raising():
    sink = OosSignalCaptureSink()
    sink.mark_invalid("unsupported_batch_shape")
    sink.finalize(set())
    wf_result = _walkforward_result_no_selection("S1")
    evidence = build_strategy_evidence(
        strategy="S1",
        walkforward_result=wf_result,
        capture_sink=sink,
        signal_concurrency_evidence=_concurrency("S1"),
        bars_1m=_flat_bars_1m(),
        funding_sidecars=_flat_funding_sidecars(),
        gap_ranges=_empty_gap_ranges(),
    )
    assert evidence["capture_valid"] is False
    assert "pbo_valid" not in evidence


def test_pbo_failure_propagates_never_swallowed():
    from rob944_walkforward import MissingSymbolDataError

    sink = OosSignalCaptureSink()
    sink.finalize(set())
    wf_result = _walkforward_result_no_selection("S1")
    bars_1m = _flat_bars_1m()
    del bars_1m[
        "BTCUSDT"
    ]  # missing symbol -> compute_pbo_evidence_for_strategy must raise
    try:
        build_strategy_evidence(
            strategy="S1",
            walkforward_result=wf_result,
            capture_sink=sink,
            signal_concurrency_evidence=_concurrency("S1"),
            bars_1m=bars_1m,
            funding_sidecars=_flat_funding_sidecars(),
            gap_ranges=_empty_gap_ranges(),
        )
        raise AssertionError("expected an exception to propagate, none raised")
    except MissingSymbolDataError:
        pass  # missing symbol -> universe-coverage check inside the evaluator -- propagated, not swallowed


def test_golden_path_capture_valid_and_shape():
    sink = OosSignalCaptureSink()
    sink.finalize(set())
    wf_result = _walkforward_result_no_selection("S1")
    concurrency = _concurrency("S1")
    evidence = build_strategy_evidence(
        strategy="S1",
        walkforward_result=wf_result,
        capture_sink=sink,
        signal_concurrency_evidence=concurrency,
        bars_1m=_flat_bars_1m(),
        funding_sidecars=_flat_funding_sidecars(),
        gap_ranges=_empty_gap_ranges(),
    )
    assert evidence["capture_valid"] is True
    assert set(evidence["scenarios"].keys()) == {
        "base",
        "primary_stress",
        "upward_stress",
    }
    assert len(evidence["fold_stability"]) == 8
    assert "pbo_valid" not in evidence
    assert evidence["signal_concurrency"] is concurrency
