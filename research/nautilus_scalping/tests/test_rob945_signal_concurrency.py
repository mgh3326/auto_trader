"""ROB-945 (H5) -- cross-symbol 1m signal-concurrency authority RED tests.

Frozen by Fable Q1=A (orch-fable-answer-rob945-20260718.md, 2026-07-18):
signal-active-minute collision rate over the validated, pre-funding/
pre-engine OOS ``SignalEvent`` batch. canonical event key =
``(strategy, fold_id, symbol, signal_ts)``; denominator = unique
``(strategy, fold_id, minute)`` with >=1 distinct symbol signal; numerator =
same with >=2 distinct symbols; rate = numerator/denominator;
denominator=0 -> JSON null + ``no_entry_signal_minutes``; histogram over
``distinct_symbol_count`` buckets 1..4; overall row sums per-strategy
numerator/denominator (reference only, no separate pass rule). Captain
adversarial review (2026-07-18): the input contract is EXACTLY the frozen
{"S1", "S2"} strategy keys and the frozen 4-symbol universe, and output
ordering must be deterministic regardless of the caller's mapping
insertion order.
"""

from __future__ import annotations

import pytest
from rob940_engine import SignalEvent
from rob945_signal_concurrency import compute_signal_concurrency


def _sig(strategy, fold_id, symbol, signal_ts, config_id="S1-00"):
    return SignalEvent(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        signal_ts=signal_ts,
        side="long",
        sl_distance_bps=200.0,
        tp_distance_bps=300.0,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id=fold_id,
    )


def _both_strategies(s1_signals=(), s2_signals=()):
    return {"S1": list(s1_signals), "S2": list(s2_signals)}


def test_two_symbols_same_minute_same_fold_is_one_collision_minute():
    signals = _both_strategies(
        s1_signals=[
            _sig("S1", "fold-00", "BTCUSDT", 1_000),
            _sig("S1", "fold-00", "XRPUSDT", 1_000),
        ]
    )
    report = compute_signal_concurrency(signals)
    s1 = report.per_strategy_by_name["S1"]
    assert s1.denominator == 1
    assert s1.numerator == 1
    assert s1.rate == 1.0


def test_disjoint_minutes_never_collide():
    signals = _both_strategies(
        s1_signals=[
            _sig("S1", "fold-00", "BTCUSDT", 1_000),
            _sig("S1", "fold-00", "XRPUSDT", 2_000),
        ]
    )
    report = compute_signal_concurrency(signals)
    s1 = report.per_strategy_by_name["S1"]
    assert s1.denominator == 2
    assert s1.numerator == 0
    assert s1.rate == 0.0


def test_same_timestamp_different_fold_never_merges():
    """A coincidental identical absolute signal_ts across two DIFFERENT
    folds must count as two separate minute-keys, never one collision."""
    signals = _both_strategies(
        s1_signals=[
            _sig("S1", "fold-00", "BTCUSDT", 1_000),
            _sig("S1", "fold-01", "XRPUSDT", 1_000),
        ]
    )
    report = compute_signal_concurrency(signals)
    s1 = report.per_strategy_by_name["S1"]
    assert s1.denominator == 2
    assert s1.numerator == 0


def test_zero_denominator_keeps_denominator_zero_with_null_rate_and_stable_reason():
    """Final ruling (orch-fable-answer-rob945c-20260718.md, Q1=A FINAL): the
    zero-signal row preserves the observed count (denominator=0, a plain
    int, never null) -- only the RATE (0/0) is undefined."""
    report = compute_signal_concurrency(_both_strategies())
    s1 = report.per_strategy_by_name["S1"]
    assert s1.denominator == 0
    assert type(s1.denominator) is int
    assert s1.rate is None
    assert s1.reason == "no_entry_signal_minutes"


def test_distinct_symbol_count_histogram_buckets_1_through_4():
    signals = _both_strategies(
        s1_signals=[
            _sig("S1", "fold-00", "BTCUSDT", 1_000),  # minute A: 1 symbol
            _sig("S1", "fold-00", "BTCUSDT", 2_000),  # minute B: 2 symbols
            _sig("S1", "fold-00", "XRPUSDT", 2_000),
            _sig("S1", "fold-00", "BTCUSDT", 3_000),  # minute C: 4 symbols
            _sig("S1", "fold-00", "XRPUSDT", 3_000),
            _sig("S1", "fold-00", "DOGEUSDT", 3_000),
            _sig("S1", "fold-00", "SOLUSDT", 3_000),
        ]
    )
    report = compute_signal_concurrency(signals)
    s1 = report.per_strategy_by_name["S1"]
    assert s1.distinct_symbol_count_histogram == {1: 1, 2: 1, 3: 0, 4: 1}
    assert s1.denominator == 3
    assert s1.numerator == 2  # minutes B and C have >=2 distinct symbols


def test_s2_rejection_is_not_an_entry_signal_and_is_excluded():
    """This module's input contract is the already-validated ACCEPTED
    SignalEvent stream only -- a generator's own pre-execution rejection
    (e.g. S2's target_direction_invalid) must never reach this function as
    if it were an entry signal. Since ``NoTradeRecord`` and ``SignalEvent``
    are distinct types, passing a NoTradeRecord-shaped object must fail
    closed rather than being silently counted."""
    from rob940_engine import NoTradeRecord

    rejection = NoTradeRecord(
        strategy="S2",
        config_id="S2-00",
        symbol="BTCUSDT",
        side="long",
        signal_ts=1_000,
        reason="target_direction_invalid",
        fold_id="fold-00",
    )
    with pytest.raises((TypeError, ValueError)):
        compute_signal_concurrency(_both_strategies(s2_signals=[rejection]))


def test_overall_row_sums_per_strategy_numerators_and_denominators_reference_only():
    signals = _both_strategies(
        s1_signals=[
            _sig("S1", "fold-00", "BTCUSDT", 1_000),
            _sig("S1", "fold-00", "XRPUSDT", 1_000),
        ],
        s2_signals=[_sig("S2", "fold-00", "BTCUSDT", 5_000)],
    )
    report = compute_signal_concurrency(signals)
    assert report.overall_numerator == 1  # only S1's collision minute
    assert report.overall_denominator == 2  # S1:1 + S2:1


def test_scenario_input_never_triple_counts_signals():
    """13/17/22bp cost scenarios share the SAME OOS signal input -- this
    module must never be handed (or asked to fan out over) a per-scenario
    signal stream; its whole-input contract is exactly one signal set per
    strategy, counted exactly once."""
    s1_signals = [
        _sig("S1", "fold-00", "BTCUSDT", 1_000),
        _sig("S1", "fold-00", "XRPUSDT", 1_000),
    ]
    report_once = compute_signal_concurrency(_both_strategies(s1_signals=s1_signals))
    # Feeding the same signals in twice must fail closed (duplicate
    # canonical event key), not silently double/triple count them.
    with pytest.raises(ValueError):
        compute_signal_concurrency(_both_strategies(s1_signals=s1_signals + s1_signals))
    assert report_once.per_strategy_by_name["S1"].denominator == 1


def test_duplicate_canonical_event_key_fails_closed():
    signals = _both_strategies(
        s1_signals=[
            _sig("S1", "fold-00", "BTCUSDT", 1_000),
            _sig("S1", "fold-00", "BTCUSDT", 1_000),
        ]
    )
    with pytest.raises(ValueError):
        compute_signal_concurrency(signals)


def test_missing_strategy_key_fails_closed():
    with pytest.raises(ValueError):
        compute_signal_concurrency({"S1": []})


def test_extra_unknown_strategy_key_fails_closed():
    with pytest.raises(ValueError):
        compute_signal_concurrency({"S1": [], "S2": [], "S3": []})


def test_unknown_symbol_outside_the_frozen_universe_fails_closed():
    signals = _both_strategies(s1_signals=[_sig("S1", "fold-00", "NOTACOIN", 1_000)])
    with pytest.raises(ValueError):
        compute_signal_concurrency(signals)


def test_output_strategy_ordering_is_deterministic_regardless_of_input_mapping_order():
    forward = _both_strategies(
        s1_signals=[_sig("S1", "fold-00", "BTCUSDT", 1_000)],
        s2_signals=[_sig("S2", "fold-00", "XRPUSDT", 2_000)],
    )
    reversed_input = {"S2": forward["S2"], "S1": forward["S1"]}
    report_forward = compute_signal_concurrency(forward)
    report_reversed = compute_signal_concurrency(reversed_input)
    assert [e.strategy for e in report_forward.per_strategy] == ["S1", "S2"]
    assert [e.strategy for e in report_reversed.per_strategy] == ["S1", "S2"]
