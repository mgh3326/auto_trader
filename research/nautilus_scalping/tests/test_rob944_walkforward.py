"""ROB-944 (H4, ROB-940) — walk-forward runner mechanics tests.

Uses small, fully-controlled synthetic bars/signals (not the real H3 signal
math, which is already covered by test_rob940_signal_s1/s2.py) to exercise
the walk-forward CONTRACT in isolation: train/OOS slicing (no leak), train-
only selection (no reselection from OOS), canonical concatenation, child
crash/timeout terminal evidence, gap-in-position reclassification, the
funding entry gate wired in before H2, and per-config attempt accounting.

Synthetic folds span several CALENDAR DAYS (not just minutes) so that each
fold's TRAIN window can produce >= MIN_SYMBOL_TRAIN_TRADES (5) completed
trades per symbol without hitting ``rob940_engine.DAILY_MAX_ENTRIES`` (3) --
trades are placed 2-per-calendar-day across the zone's span.
"""

from __future__ import annotations

import pytest
import rob944_walkforward
from rob940_cost_model import COST_SCENARIO_PRIMARY_STRESS, COST_SCENARIOS
from rob940_engine import Bar1m, NoTradeRecord, SignalEvent
from rob941_funding_sidecar import FundingSidecar
from rob944_diagnostic_evidence import ChildFailureEvidence
from rob944_folds import Fold
from rob944_walkforward import (
    REASON_CHILD_EXECUTION_CRASHED,
    REASON_CHILD_EXECUTION_TIMEOUT,
    REASON_DATA_GAP_IN_POSITION,
    ConfigSpec,
    ForgedSignalError,
    GeneratedSignalBatch,
    MissingSymbolDataError,
    _combine_static_and_signals,
    _evaluate_fold_oos,
    _evaluate_fold_train,
    _json_safe_funding_rate,
    _run_scenario,
    _validate_generated_rejections,
    run_walkforward,
    summarize_config_attempts_for_h6,
)

from research_contracts.canonical_hash import canonical_sha256

_SYMBOLS = ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")
_DAY_MS = 86_400_000
_HOUR_MS = 3_600_000


def _bar(ts, price=100.0):
    return Bar1m(ts=ts, open=price, high=price, low=price, close=price, volume=1.0)


def _flat_bars(start_ms, end_ms, overrides: dict[int, float] | None = None):
    overrides = overrides or {}
    out = []
    ts = start_ms
    while ts < end_ms:
        out.append(_bar(ts, overrides.get(ts, 100.0)))
        ts += 60_000
    return tuple(out)


def _permissive_funding_sidecars():
    from funding_oi_archive import FundingRow

    return {
        s: FundingSidecar.from_rows(
            s,
            [
                FundingRow(
                    calc_time=-10_000_000,
                    funding_interval_hours=8,
                    last_funding_rate=0.0,
                )
            ],
        )
        for s in _SYMBOLS
    }


def _no_gaps():
    return dict.fromkeys(_SYMBOLS, ())


# Two contiguous, non-overlapping folds spanning several calendar days: train
# 3 days / embargo 3h / OOS 2 days / roll 2 days (test-only spans -- NOT the
# frozen 120d/3h/28d/28d production contract, which is covered separately by
# test_rob944_folds.py; only the mechanics under test here need these spans).
_FOLD_0 = Fold(
    fold_id="fold-00",
    fold_index=0,
    train_start_ms=0,
    train_end_ms=3 * _DAY_MS,
    embargo_start_ms=3 * _DAY_MS,
    embargo_end_ms=3 * _DAY_MS + 3 * _HOUR_MS,
    oos_start_ms=3 * _DAY_MS + 3 * _HOUR_MS,
    oos_end_ms=3 * _DAY_MS + 3 * _HOUR_MS + 2 * _DAY_MS,
)
_ROLL_MS = 2 * _DAY_MS
_FOLD_1 = Fold(
    fold_id="fold-01",
    fold_index=1,
    train_start_ms=_ROLL_MS,
    train_end_ms=_ROLL_MS + 3 * _DAY_MS,
    embargo_start_ms=_ROLL_MS + 3 * _DAY_MS,
    embargo_end_ms=_ROLL_MS + 3 * _DAY_MS + 3 * _HOUR_MS,
    oos_start_ms=_ROLL_MS + 3 * _DAY_MS + 3 * _HOUR_MS,
    oos_end_ms=_ROLL_MS + 3 * _DAY_MS + 3 * _HOUR_MS + 2 * _DAY_MS,
)
_WINDOW_END = _FOLD_1.oos_end_ms

assert _FOLD_0.oos_end_ms == _FOLD_1.oos_start_ms  # contiguous, non-overlapping


def _make_signal(
    symbol,
    signal_ts,
    side="long",
    config_id="S1-00",
    fold_id=None,
    sl=200.0,
    tp=300.0,
    timeout_bars=1,
):
    return SignalEvent(
        strategy="S1",
        config_id=config_id,
        symbol=symbol,
        signal_ts=signal_ts,
        side=side,
        sl_distance_bps=sl,
        tp_distance_bps=tp,
        timeout_bars=timeout_bars,
        cooldown_bars=0,
        fold_id=fold_id,
    )


def _entries_in_zone(zone_start_ms, zone_end_ms):
    """Two entry timestamps per calendar day within ``[zone_start, zone_end)``
    -- each entry needs its own bar plus a deadline bar one minute later, both
    within the SAME calendar day (rob940_engine's daily-entry-cap/halt
    accounting keys off the entry bar's own UTC date) and within the zone.
    """
    entries = []
    day_start = (zone_start_ms // _DAY_MS) * _DAY_MS
    while day_start < zone_end_ms:
        day_end = day_start + _DAY_MS
        iter_start = max(day_start, zone_start_ms)
        window_end = min(day_end, zone_end_ms)
        for offset in (0, 120_000):
            entry_ts = iter_start + offset
            deadline_ts = entry_ts + 60_000
            if entry_ts < window_end and deadline_ts <= window_end:
                entries.append(entry_ts)
        day_start += _DAY_MS
    return entries


def _controlled_bars_and_generator(config_id, *, train_price, oos_price):
    """Build a full flat bars_1m array (shared across all configs, since
    market data isn't per-config) with 2 controlled entry/deadline trade
    pairs per calendar day in EACH fold's train/oos zones (``train_price``
    used for both folds' train zones, ``oos_price`` for both folds' oos
    zones), and a matching generator that emits signals at those same
    entries whenever they fall inside whatever bars_slice it's given.
    """
    zones = [
        (_FOLD_0.train_start_ms, _FOLD_0.train_end_ms, train_price),
        (_FOLD_0.oos_start_ms, _FOLD_0.oos_end_ms, oos_price),
        (_FOLD_1.train_start_ms, _FOLD_1.train_end_ms, train_price),
        (_FOLD_1.oos_start_ms, _FOLD_1.oos_end_ms, oos_price),
    ]
    overrides: dict[int, float] = {}
    entry_set: set[int] = set()
    for zone_start, zone_end, price in zones:
        for entry_ts in _entries_in_zone(zone_start, zone_end):
            overrides[entry_ts + 60_000] = price
            entry_set.add(entry_ts)

    bars_1m = {symbol: _flat_bars(0, _WINDOW_END, overrides) for symbol in _SYMBOLS}

    def _gen(symbol, bars_slice, fold_id):
        present = {b.ts for b in bars_slice}
        return tuple(
            _make_signal(symbol, ts, config_id=config_id, fold_id=fold_id)
            for ts in sorted(entry_set)
            if ts in present
        )

    return bars_1m, _gen


def _twelve_configs(
    winner_id,
    *,
    winner_train_price,
    winner_oos_price,
    loser_train_price=100.0,
    loser_oos_price=100.0,
):
    """12 uniquely-ided ConfigSpecs; ``winner_id`` gets the caller-controlled
    prices, every other config is flat (deadline price == entry price ==
    100 -> every trade times out with zero gross move, net_bps is
    cost-drag-negative but IDENTICAL and small for every flat config)."""
    bars_by_config: dict[str, dict[str, tuple[Bar1m, ...]]] = {}
    specs = []
    for i in range(12):
        cid = f"S1-{i:02d}"
        if cid == winner_id:
            bars, gen = _controlled_bars_and_generator(
                cid, train_price=winner_train_price, oos_price=winner_oos_price
            )
        else:
            bars, gen = _controlled_bars_and_generator(
                cid, train_price=loser_train_price, oos_price=loser_oos_price
            )
        bars_by_config[cid] = bars
        specs.append(ConfigSpec(config_id=cid, generate_signals=gen))
    return specs, bars_by_config


def test_train_and_oos_signal_generators_only_see_their_own_window_bars():
    seen_windows: list[tuple[str, int, int]] = []

    def _recording_gen(symbol, bars_slice, fold_id):
        seen_windows.append((fold_id, bars_slice[0].ts, bars_slice[-1].ts))
        return ()

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_recording_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    for fold_id, first_ts, last_ts in seen_windows:
        if fold_id == "fold-00":
            in_train = (
                first_ts >= _FOLD_0.train_start_ms and last_ts < _FOLD_0.train_end_ms
            )
            in_oos = first_ts >= _FOLD_0.oos_start_ms and last_ts < _FOLD_0.oos_end_ms
            assert in_train or in_oos, (fold_id, first_ts, last_ts)
        if fold_id == "fold-01":
            in_train = (
                first_ts >= _FOLD_1.train_start_ms and last_ts < _FOLD_1.train_end_ms
            )
            in_oos = first_ts >= _FOLD_1.oos_start_ms and last_ts < _FOLD_1.oos_end_ms
            assert in_train or in_oos, (fold_id, first_ts, last_ts)
        # never straddling the embargo of either fold.
        assert not (_FOLD_0.embargo_start_ms <= first_ts < _FOLD_0.embargo_end_ms)
        assert not (_FOLD_1.embargo_start_ms <= first_ts < _FOLD_1.embargo_end_ms)


def test_selection_is_train_only_oos_reversal_does_not_change_the_winner():
    """Config WINNER looks great on TRAIN and bad on OOS; config LOSER looks
    bad on TRAIN and great on OOS. The fold must still select WINNER (train
    evidence only) and then RUN winner's own (bad) OOS -- never reselect
    based on the OOS numbers."""
    specs = []
    bars_by_config = {}
    for i in range(12):
        cid = f"S1-{i:02d}"
        if cid == "S1-00":
            bars, gen = _controlled_bars_and_generator(
                cid, train_price=110.0, oos_price=90.0
            )
        elif cid == "S1-01":
            bars, gen = _controlled_bars_and_generator(
                cid, train_price=90.0, oos_price=110.0
            )
        else:
            bars, gen = _controlled_bars_and_generator(
                cid, train_price=100.0, oos_price=100.0
            )
        bars_by_config[cid] = bars
        specs.append(ConfigSpec(config_id=cid, generate_signals=gen))

    # Fake generators derive prices from their OWN closures (not from
    # `bars_1m`'s content); the runner's `bars_1m` argument just needs to be
    # long/minute-aligned enough for the loop -- use the winner's own bars.
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    for fold_result in result.folds:
        assert fold_result.selection_trace.selected_config_id == "S1-00"
    primary_ledger = result.concatenated_oos_ledgers["primary_stress"]
    assert primary_ledger  # WINNER's (bad) OOS still produced trades
    assert all(t.config_id == "S1-00" for t in primary_ledger)


def test_concatenated_oos_ledger_is_canonically_ordered_and_folds_never_overlap():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    ledger = result.concatenated_oos_ledgers["primary_stress"]
    entry_ts_values = [t.entry_ts for t in ledger]
    assert entry_ts_values == sorted(entry_ts_values)
    fold00_entries = [t for t in ledger if t.entry_ts < _FOLD_1.oos_start_ms]
    fold01_entries = [t for t in ledger if t.entry_ts >= _FOLD_1.oos_start_ms]
    assert fold00_entries and fold01_entries
    assert max(t.entry_ts for t in fold00_entries) < min(
        t.entry_ts for t in fold01_entries
    )


def test_a_config_that_never_wins_any_fold_still_has_a_completed_attempt():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    loser_attempt = next(a for a in result.config_attempts if a.config_id == "S1-01")
    assert loser_attempt.status == "completed"
    assert loser_attempt.selected_in_folds == ()

    winner_attempt = next(a for a in result.config_attempts if a.config_id == "S1-00")
    assert winner_attempt.status == "completed"
    assert winner_attempt.selected_in_folds == ("fold-00", "fold-01")


def test_exactly_twelve_config_attempts_in_original_input_order():
    specs, bars_by_config = _twelve_configs(
        "S1-05", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-05"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert [a.config_id for a in result.config_attempts] == [
        f"S1-{i:02d}" for i in range(12)
    ]


def test_child_generator_exception_produces_crashed_attempt_not_silent_skip():
    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError("boom: synthetic signal-generation failure")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_raising_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert all(a.status == "crashed" for a in result.config_attempts)
    assert all(a.crash_log for a in result.config_attempts)
    assert len(result.config_attempts) == 12


def test_data_gap_in_position_excludes_the_trade_from_the_oos_ledger():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    fold0_oos_entries = sorted(
        _entries_in_zone(_FOLD_0.oos_start_ms, _FOLD_0.oos_end_ms)
    )
    fold1_oos_entries = sorted(
        _entries_in_zone(_FOLD_1.oos_start_ms, _FOLD_1.oos_end_ms)
    )
    # Pick a fold-00 OOS entry that does NOT also fall inside fold-01's TRAIN
    # zone (fold-00's OOS span [oos_start, oos_end) partially overlaps
    # fold-01's train span, since folds roll) -- otherwise the "same" gap
    # would ALSO invalidate fold-01's train evidence and change (or remove)
    # its own winner, contaminating this test's assumption that fold-01 is
    # unaffected.
    gapped_entry = next(ts for ts in fold0_oos_entries if ts >= _FOLD_1.train_end_ms)
    # A gap squarely inside that ONE trade's [entry, exit) window.
    gap_ranges = dict.fromkeys(_SYMBOLS, ((gapped_entry, gapped_entry + 30000),))

    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=gap_ranges,
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    ledger = result.concatenated_oos_ledgers["primary_stress"]
    assert all(t.entry_ts != gapped_entry for t in ledger)
    # An OOS trade from fold-01 (unaffected window) must still be present.
    assert any(t.entry_ts == fold1_oos_entries[0] for t in ledger)


def test_funding_gate_rejection_prevents_a_trade_without_silently_vanishing_it():
    from funding_oi_archive import FundingRow

    # A 2-hour hold window (timeout_bars=120) against an HOURLY funding
    # interval guarantees at least one relevant crossing after every entry --
    # a reliably "hostile" sidecar regardless of exact entry alignment.
    def _long_hold_gen(symbol, bars_slice, fold_id):
        if not bars_slice:
            return ()
        return (
            SignalEvent(
                strategy="S1",
                config_id="S1-00",
                symbol=symbol,
                signal_ts=bars_slice[0].ts,
                side="long",
                sl_distance_bps=200.0,
                tp_distance_bps=300.0,
                timeout_bars=120,
                cooldown_bars=0,
                fold_id=fold_id,
            ),
        )

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_long_hold_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    # A funding sidecar whose known rate implies a large expected LONG cost
    # at every relevant entry (0.01 -> 100bp signed cost, far above 3bp).
    hostile_sidecars = {
        s: FundingSidecar.from_rows(
            s,
            [
                FundingRow(
                    calc_time=-10, funding_interval_hours=1, last_funding_rate=0.01
                )
            ],
        )
        for s in _SYMBOLS
    }
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=hostile_sidecars,
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    ledger = result.concatenated_oos_ledgers["primary_stress"]
    assert ledger == ()  # every signal was funding-gated out, not silently traded
    winner_attempt = next(a for a in result.config_attempts if a.config_id == "S1-00")
    # The gate rejection isn't a crash -- but with EVERY train trade also
    # funding-gated out, no symbol ever reaches MIN_SYMBOL_TRAIN_TRADES, so
    # every config is rejected in every fold (never eligible anywhere).
    assert winner_attempt.status == "rejected"
    assert winner_attempt.reason_code == "insufficient_train_evidence_all_folds"
    assert not winner_attempt.crash_log


def test_three_cost_scenarios_are_each_present_in_oos_results():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert set(result.concatenated_oos_ledgers.keys()) == {
        s.name for s in COST_SCENARIOS
    }


# ---------------------------------------------------------------------------
# summarize_config_attempts_for_h6 -- captain security/determinism correction
# (2026-07-17): fixed reason codes only (never raw exception/log text).
# ---------------------------------------------------------------------------


def test_summarize_never_selected_config_gets_sentinel_hash_zero_trades():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries = summarize_config_attempts_for_h6(result)
    loser = next(s for s in summaries if s.config_id == "S1-01")
    assert loser.status == "completed"
    assert loser.reason_code is None
    for row in loser.scenario_summaries:
        assert row.status == "never_selected"
        assert row.trade_count == 0
        assert len(row.artifact_hash) == 64


def test_summarize_selected_config_combines_all_won_folds_trade_counts():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries = summarize_config_attempts_for_h6(result)
    winner = next(s for s in summaries if s.config_id == "S1-00")
    assert winner.status == "completed"
    primary = next(
        row
        for row in winner.scenario_summaries
        if row.scenario_name == "primary_stress"
    )
    # Won BOTH folds, 4 symbols each -> trades summed across both folds/symbols.
    total_primary_trades = sum(
        1
        for t in result.concatenated_oos_ledgers["primary_stress"]
        if t.config_id == "S1-00"
    )
    assert primary.trade_count == total_primary_trades
    assert primary.trade_count > 0
    assert primary.status == "completed"


def test_summarize_exactly_three_scenario_rows_covering_all_scenario_names():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries = summarize_config_attempts_for_h6(result)
    assert len(summaries) == 12
    for summary in summaries:
        assert len(summary.scenario_summaries) == 3
        assert {row.scenario_name for row in summary.scenario_summaries} == {
            s.name for s in COST_SCENARIOS
        }


def test_summarize_crashed_attempt_gets_fixed_reason_code_not_raw_text():
    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError("simulated child failure")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_raising_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries = summarize_config_attempts_for_h6(result)
    assert all(s.status == "crashed" for s in summaries)
    assert all(s.reason_code == REASON_CHILD_EXECUTION_CRASHED for s in summaries)
    assert all(
        "simulated child failure" not in (s.reason_code or "") for s in summaries
    )


def test_summarize_timeout_only_attempt_gets_fixed_timeout_reason_code():
    def _timing_out_gen(symbol, bars_slice, fold_id):
        raise TimeoutError("simulated child timeout")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_timing_out_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries = summarize_config_attempts_for_h6(result)
    assert all(s.status == "timeout" for s in summaries)
    assert all(s.reason_code == REASON_CHILD_EXECUTION_TIMEOUT for s in summaries)


def test_summarize_sentinel_secret_never_leaks_into_evidence_or_hashes():
    """A crash/timeout exception message that LOOKS like a leaked credential
    must never surface anywhere in the returned evidence -- not in
    reason_code, not (in readable form) in any scenario artifact hash."""
    sentinel = "sk-live-SUPERSECRETTOKEN-should-never-appear-in-evidence"

    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError(f"DB connect failed: password={sentinel}")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_raising_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries = summarize_config_attempts_for_h6(result)
    for summary in summaries:
        assert sentinel not in (summary.reason_code or "")
        for row in summary.scenario_summaries:
            assert sentinel not in row.artifact_hash
            assert row.artifact_hash != sentinel


def test_summarize_is_deterministic_across_repeated_calls():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summary1 = summarize_config_attempts_for_h6(result)
    summary2 = summarize_config_attempts_for_h6(result)
    assert summary1 == summary2


# ---------------------------------------------------------------------------
# captain core-semantic corrections: exact 4-symbol universe coverage
# fail-closed; forged/out-of-window signal identity fail-closed; gap
# rejection propagates to the WHOLE logical attempt.
# ---------------------------------------------------------------------------


def _valid_full_kwargs():
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    return {
        "strategy": "S1",
        "configs": tuple(specs),
        "bars_1m": bars_by_config["S1-00"],
        "funding_sidecars": _permissive_funding_sidecars(),
        "gap_ranges": _no_gaps(),
        "fold_schedule": (_FOLD_0, _FOLD_1),
    }


def test_missing_symbol_in_bars_1m_fails_closed_before_any_fold_work():
    kwargs = _valid_full_kwargs()
    bars_1m = dict(kwargs["bars_1m"])
    del bars_1m["SOLUSDT"]
    kwargs["bars_1m"] = bars_1m
    with pytest.raises(MissingSymbolDataError):
        run_walkforward(**kwargs)


def test_missing_symbol_in_funding_sidecars_fails_closed():
    kwargs = _valid_full_kwargs()
    sidecars = dict(kwargs["funding_sidecars"])
    del sidecars["SOLUSDT"]
    kwargs["funding_sidecars"] = sidecars
    with pytest.raises(MissingSymbolDataError):
        run_walkforward(**kwargs)


def test_missing_symbol_in_gap_ranges_fails_closed():
    kwargs = _valid_full_kwargs()
    gaps = dict(kwargs["gap_ranges"])
    del gaps["SOLUSDT"]
    kwargs["gap_ranges"] = gaps
    with pytest.raises(MissingSymbolDataError):
        run_walkforward(**kwargs)


def test_extra_symbol_beyond_frozen_universe_fails_closed():
    kwargs = _valid_full_kwargs()
    bars_1m = dict(kwargs["bars_1m"])
    bars_1m["ETHUSDT"] = bars_1m["BTCUSDT"]
    kwargs["bars_1m"] = bars_1m
    with pytest.raises(MissingSymbolDataError):
        run_walkforward(**kwargs)


def test_forged_signal_identity_symbol_is_terminal_crash_evidence():
    """A generator requested for symbol=BTCUSDT that returns a SignalEvent
    forged under a DIFFERENT symbol must never be silently trusted/relabeled
    -- it is terminal (crashed) invalid-data evidence."""

    def _forging_gen(symbol, bars_slice, fold_id):
        if not bars_slice:
            return ()
        real_bar = bars_slice[0]
        forged_symbol = "XRPUSDT" if symbol == "BTCUSDT" else symbol
        return (
            SignalEvent(
                strategy="S1",
                config_id="S1-00",
                symbol=forged_symbol,
                signal_ts=real_bar.ts,
                side="long",
                sl_distance_bps=200.0,
                tp_distance_bps=300.0,
                timeout_bars=1,
                cooldown_bars=0,
                fold_id=fold_id,
            ),
        )

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_forging_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert all(a.status == "crashed" for a in result.config_attempts)
    assert all(a.crash_log for a in result.config_attempts)
    # The forgery must never leak a mislabeled trade into any ledger.
    for ledger in result.concatenated_oos_ledgers.values():
        assert ledger == ()


def test_forged_signal_identity_config_id_is_terminal_crash_evidence():
    def _forging_gen(symbol, bars_slice, fold_id):
        if not bars_slice:
            return ()
        return (
            SignalEvent(
                strategy="S1",
                config_id="S1-99-FORGED",
                symbol=symbol,
                signal_ts=bars_slice[0].ts,
                side="long",
                sl_distance_bps=200.0,
                tp_distance_bps=300.0,
                timeout_bars=1,
                cooldown_bars=0,
                fold_id=fold_id,
            ),
        )

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_forging_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert all(a.status == "crashed" for a in result.config_attempts)


def test_signal_ts_before_window_start_is_rejected_as_forged():
    def _early_gen(symbol, bars_slice, fold_id):
        return (
            SignalEvent(
                strategy="S1",
                config_id="S1-00",
                symbol=symbol,
                signal_ts=-1,
                side="long",
                sl_distance_bps=200.0,
                tp_distance_bps=300.0,
                timeout_bars=1,
                cooldown_bars=0,
                fold_id=fold_id,
            ),
        )

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_early_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert all(a.status == "crashed" for a in result.config_attempts)


def test_signal_ts_after_window_end_is_rejected_as_forged():
    def _late_gen(symbol, bars_slice, fold_id):
        return (
            SignalEvent(
                strategy="S1",
                config_id="S1-00",
                symbol=symbol,
                signal_ts=_FOLD_1.oos_end_ms + 60_000,
                side="long",
                sl_distance_bps=200.0,
                tp_distance_bps=300.0,
                timeout_bars=1,
                cooldown_bars=0,
                fold_id=fold_id,
            ),
        )

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_late_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert all(a.status == "crashed" for a in result.config_attempts)


def test_signal_ts_exactly_at_window_end_is_a_legitimate_close_boundary_not_forged():
    """A legitimate H3 close-boundary signal (aggregated bar close_ts ==
    train_end/oos_end exactly) must NOT be rejected as forged -- it simply
    finds no entry bar in the half-open [start, end) slice and H2 reports
    next_bar_unavailable, never crossing into the embargo/next fold."""

    def _boundary_gen_factory(config_id):
        def _gen(symbol, bars_slice, fold_id):
            if not bars_slice:
                return ()
            # signal_ts == the slice's own end boundary (one bar-width past
            # the last bar actually in bars_slice, since the slice is
            # [start, end)).
            boundary_ts = bars_slice[-1].ts + 60_000
            return (
                SignalEvent(
                    strategy="S1",
                    config_id=config_id,
                    symbol=symbol,
                    signal_ts=boundary_ts,
                    side="long",
                    sl_distance_bps=200.0,
                    tp_distance_bps=300.0,
                    timeout_bars=1,
                    cooldown_bars=0,
                    fold_id=fold_id,
                ),
            )

        return _gen

    specs = [
        ConfigSpec(
            config_id=f"S1-{i:02d}",
            generate_signals=_boundary_gen_factory(f"S1-{i:02d}"),
        )
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    # Never treated as forged (no crash) -- just structurally ineligible
    # (0 completed trades everywhere -> insufficient train evidence).
    assert all(a.status != "crashed" for a in result.config_attempts)
    assert all(not a.crash_log for a in result.config_attempts)


def test_gap_rejection_makes_the_whole_attempt_rejected_not_completed():
    """A scenario-level gap rejection (train or OOS) must make the WHOLE
    logical config attempt status="rejected" with
    rejected:data_gap_in_position -- never remain "completed" merely because
    the config was train-eligible somewhere."""
    specs, bars_by_config = _twelve_configs(
        "S1-00", winner_train_price=110.0, winner_oos_price=105.0
    )
    fold0_oos_entries = sorted(
        _entries_in_zone(_FOLD_0.oos_start_ms, _FOLD_0.oos_end_ms)
    )
    gapped_entry = next(ts for ts in fold0_oos_entries if ts >= _FOLD_1.train_end_ms)
    gap_ranges = dict.fromkeys(_SYMBOLS, ((gapped_entry, gapped_entry + 30_000),))

    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_by_config["S1-00"],
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=gap_ranges,
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    winner_attempt = next(a for a in result.config_attempts if a.config_id == "S1-00")
    assert winner_attempt.status == "rejected"
    assert winner_attempt.reason_code == REASON_DATA_GAP_IN_POSITION
    assert winner_attempt.gap_rejection_log
    assert not winner_attempt.crash_log


# ---------------------------------------------------------------------------
# Q4 addendum (captain, 2026-07-17): "merely having 3 output keys does not
# kill a one-path revaluer mutation" -- an H4-level spy on
# rob944_walkforward.run_symbol_stream must prove EXACTLY 3 FRESH
# invocations per (symbol, fold) OOS evaluation (one per COST_SCENARIOS
# entry), each receiving the IDENTICAL raw (bars, signals) input -- not one
# real run whose ledger is then rescaled/derived for the other two. Reuses
# H2's own known 3/3/2 trade-count divergence + scenario-independent 68bp
# gate fixture (test_rob940_engine.test_cost_scenario_dependent_daily_stop_diverges_trade_count)
# verbatim, so this is provably the SAME divergence H2 already owns, not a
# new/different H4 reimplementation of it.
# ---------------------------------------------------------------------------


def _h2_repro_bars_and_signals(base_ts):
    """Verbatim reproduction of H2's 3/3/2 divergence fixture (trade1 SL
    touch, trade2 timeout, trade3 clean gap-through TP), re-anchored at
    ``base_ts`` instead of ts=0 so it can be placed inside an OOS window."""
    specs = [
        (100, 100, 100, 100),
        (91, 91.5, 90, 90.5),
        (100, 100, 100, 100),
        (99.97, 99.97, 99.97, 99.97),
        (100, 100, 100, 100),
        (102, 102, 102, 102),
    ]
    bars = tuple(
        Bar1m(ts=base_ts + i * 60_000, open=o, high=h, low=lo, close=c, volume=1.0)
        for i, (o, h, lo, c) in enumerate(specs)
    )
    sig1 = SignalEvent(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        signal_ts=base_ts,
        side="long",
        sl_distance_bps=1000.0,
        tp_distance_bps=100000.0,
        timeout_bars=5,
        cooldown_bars=0,
        fold_id="fold-00",
    )
    sig2 = SignalEvent(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        signal_ts=base_ts + 2 * 60_000,
        side="long",
        sl_distance_bps=25.0,
        tp_distance_bps=100000.0,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id="fold-00",
    )
    sig3 = SignalEvent(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        signal_ts=base_ts + 4 * 60_000,
        side="long",
        sl_distance_bps=1000.0,
        tp_distance_bps=100.0,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id="fold-00",
    )
    return bars, (sig1, sig2, sig3)


def test_h4_wires_three_fresh_independent_scenario_runs_reproducing_h2_3_3_2_divergence(
    monkeypatch,
):
    target_bars, target_signals = _h2_repro_bars_and_signals(_FOLD_0.oos_start_ms)

    bars_1m = {
        s: (
            target_bars
            if s == "BTCUSDT"
            else _flat_bars(_FOLD_0.oos_start_ms, _FOLD_0.oos_start_ms + 6 * 60_000)
        )
        for s in _SYMBOLS
    }

    def gen(symbol, bars_slice, fold_id):
        return target_signals if symbol == "BTCUSDT" else ()

    calls: list[tuple] = []
    real_run_symbol_stream = rob944_walkforward.run_symbol_stream

    def spy(bars, signals, scenario, *args, **kwargs):
        calls.append((bars, signals, scenario.name))
        return real_run_symbol_stream(bars, signals, scenario, *args, **kwargs)

    monkeypatch.setattr(rob944_walkforward, "run_symbol_stream", spy)

    outcomes, ledgers = _evaluate_fold_oos(
        "S1",
        ConfigSpec(config_id="S1-00", generate_signals=gen),
        bars_1m,
        _permissive_funding_sidecars(),
        _no_gaps(),
        _FOLD_0,
        {},
        {},
    )

    # Exactly one FRESH run_symbol_stream invocation per COST_SCENARIOS entry
    # for the target symbol -- never fewer (a shared-path revaluer collapsing
    # 3 calls into 1) and never more (a duplicate/retry leak).
    btc_calls = [c for c in calls if c[1] == target_signals]
    assert len(btc_calls) == len(COST_SCENARIOS) == 3
    assert {c[2] for c in btc_calls} == {s.name for s in COST_SCENARIOS}

    # "Fresh state" proof: every call received the IDENTICAL raw (bars,
    # signals) input -- a one-path revaluer would instead feed call N+1 some
    # DERIVED/mutated state from call N's own result (e.g. a filtered ledger,
    # a running R-multiple) rather than the same untouched inputs each time.
    for bars, signals, _name in btc_calls:
        assert bars == target_bars
        assert signals == target_signals

    # H2's own known divergence, reproduced verbatim through the H4 wiring:
    # base/primary=3 trades, upward=2 trades (AC8 cost-included daily stop).
    trade_counts = {
        name: len(ledgers[name]) for name in ("base", "primary_stress", "upward_stress")
    }
    assert trade_counts == {"base": 3, "primary_stress": 3, "upward_stress": 2}

    # The 68bp entry-eligibility gate itself is scenario-independent: all 3
    # scenarios agree on trade1/trade2 (only trade3's inclusion diverges).
    for name in ("base", "primary_stress", "upward_stress"):
        first_two_entry_ts = sorted(t.entry_ts for t in ledgers[name])[:2]
        assert first_two_entry_ts == [
            _FOLD_0.oos_start_ms,
            _FOLD_0.oos_start_ms + 2 * 60_000,
        ]


# ---------------------------------------------------------------------------
# Captain mutation-test correction (2026-07-17): the trade1/trade2 entry_ts
# equality above is VACUOUS for the 68bp-gate claim -- both signals used
# tp_distance_bps of 100bp/100000bp, far above ANY plausible scenario-scaled
# threshold (52/68/88bp), so a mutant that computes MIN_TP_DISTANCE_BPS
# per-scenario (e.g. base=52, primary=68, upward=88) would still pass both
# and this suite would never notice. This fixture uses tp_distance_bps
# EXACTLY 68.0 (eligible) and 67.99 (just below) -- verbatim H2 values from
# test_68bp_gate_is_identical_across_all_cost_scenarios -- through the SAME
# H4 _evaluate_fold_oos + run_symbol_stream-spy boundary, across all 3 REAL
# cost scenarios, to kill exactly that mutation class.
# ---------------------------------------------------------------------------


def _h2_68bp_gate_bars(base_ts):
    specs = [(100, 100, 100, 100), (100, 100.1, 99.9, 100)]
    return tuple(
        Bar1m(ts=base_ts + i * 60_000, open=o, high=h, low=lo, close=c, volume=1.0)
        for i, (o, h, lo, c) in enumerate(specs)
    )


def _h2_68bp_signal(base_ts, tp_distance_bps):
    return SignalEvent(
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        signal_ts=base_ts,
        side="long",
        sl_distance_bps=100.0,
        tp_distance_bps=tp_distance_bps,
        timeout_bars=1,
        cooldown_bars=0,
        fold_id="fold-00",
    )


def _run_68bp_case(monkeypatch, tp_distance_bps):
    bars = _h2_68bp_gate_bars(_FOLD_0.oos_start_ms)
    signal = _h2_68bp_signal(_FOLD_0.oos_start_ms, tp_distance_bps)
    bars_1m = {
        s: (
            bars
            if s == "BTCUSDT"
            else _flat_bars(_FOLD_0.oos_start_ms, _FOLD_0.oos_start_ms + 2 * 60_000)
        )
        for s in _SYMBOLS
    }

    def gen(symbol, bars_slice, fold_id):
        return (signal,) if symbol == "BTCUSDT" else ()

    calls: list[tuple] = []
    real_run_symbol_stream = rob944_walkforward.run_symbol_stream

    def spy(bars_arg, signals_arg, scenario, *args, **kwargs):
        result = real_run_symbol_stream(
            bars_arg, signals_arg, scenario, *args, **kwargs
        )
        if signals_arg == (signal,):
            calls.append((scenario.name, result))
        return result

    monkeypatch.setattr(rob944_walkforward, "run_symbol_stream", spy)

    _evaluate_fold_oos(
        "S1",
        ConfigSpec(config_id="S1-00", generate_signals=gen),
        bars_1m,
        _permissive_funding_sidecars(),
        _no_gaps(),
        _FOLD_0,
        {},
        {},
    )
    assert {name for name, _r in calls} == {s.name for s in COST_SCENARIOS}
    return dict(calls)


def test_tp_distance_exactly_68bp_is_eligible_in_all_three_independent_scenario_runs(
    monkeypatch,
):
    results = _run_68bp_case(monkeypatch, 68.0)
    for name, result in results.items():
        assert len(result.trades) == 1, name  # entry gate passed -- not rejected
        assert not result.no_trades, name


def test_tp_distance_just_below_68bp_is_rejected_in_all_three_independent_scenario_runs(
    monkeypatch,
):
    results = _run_68bp_case(monkeypatch, 67.99)
    for name, result in results.items():
        assert result.trades == (), name
        assert result.no_trades[0].reason == "tp_below_min_distance", name


# ---------------------------------------------------------------------------
# Captain freeze-audit addendum (item A) + train-input completeness/PIT-
# scope/performance follow-ups: train_artifact_hash must bind the ACTUAL
# raw TRAIN-relevant funding rows/gap ranges (never OOS-only future ones),
# fail closed (never an unaccounted exception) on non-finite funding rates,
# and do so WITHOUT re-hashing the shared bars/funding/gaps once per config
# (static fingerprint computed once per (fold, symbol), not once per
# (config, symbol)).
# ---------------------------------------------------------------------------


def _no_signal_configs(n=12):
    def _gen(symbol, bars_slice, fold_id):
        return ()

    return [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_gen) for i in range(n)
    ]


def _flat_bars_all_symbols(start_ms, end_ms):
    return {s: _flat_bars(start_ms, end_ms) for s in _SYMBOLS}


def _sidecars_with_rate(fold, rate, *, extra_rows=()):
    from funding_oi_archive import FundingRow

    rows = [
        FundingRow(
            calc_time=fold.train_start_ms + 1000,
            funding_interval_hours=8,
            last_funding_rate=rate,
        )
    ]
    rows.extend(extra_rows)
    return {s: FundingSidecar.from_rows(s, rows) for s in _SYMBOLS}


def _btc_train_hash(candidates):
    return candidates[0].symbol_evidence[0].train_artifact_hash


def _btc_train_evidence(candidates):
    return candidates[0].symbol_evidence[0]


def _configs_with_target_gen(gen, *, target_id="S1-00", n=12):
    def _empty_gen(symbol, bars_slice, fold_id):
        return ()

    return [
        ConfigSpec(
            config_id=f"S1-{i:02d}",
            generate_signals=(gen if f"S1-{i:02d}" == target_id else _empty_gen),
        )
        for i in range(n)
    ]


def test_train_relevant_funding_row_change_alters_train_artifact_hash():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    configs = _no_signal_configs()

    candidates_a = _evaluate_fold_train(
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, 0.0001),
        _no_gaps(),
        fold,
        {},
        {},
    )
    candidates_b = _evaluate_fold_train(
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, 0.0005),
        _no_gaps(),
        fold,
        {},
        {},
    )
    assert _btc_train_hash(candidates_a) != _btc_train_hash(candidates_b)


def test_oos_only_future_funding_row_does_not_alter_train_artifact_hash():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    configs = _no_signal_configs()
    from funding_oi_archive import FundingRow

    oos_only_row = FundingRow(
        calc_time=fold.oos_start_ms + 1000,
        funding_interval_hours=8,
        last_funding_rate=0.0009,
    )

    baseline = _evaluate_fold_train(
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, 0.0001),
        _no_gaps(),
        fold,
        {},
        {},
    )
    with_future_row = _evaluate_fold_train(
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, 0.0001, extra_rows=(oos_only_row,)),
        _no_gaps(),
        fold,
        {},
        {},
    )
    assert _btc_train_hash(baseline) == _btc_train_hash(with_future_row)


def test_train_relevant_gap_range_change_alters_train_artifact_hash():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    configs = _no_signal_configs()
    sidecars = _sidecars_with_rate(fold, 0.0001)
    train_gap = dict.fromkeys(
        _SYMBOLS, ((fold.train_start_ms + 500, fold.train_start_ms + 600),)
    )

    without_gap = _evaluate_fold_train(
        "S1", configs, bars_1m, sidecars, _no_gaps(), fold, {}, {}
    )
    with_gap = _evaluate_fold_train(
        "S1", configs, bars_1m, sidecars, train_gap, fold, {}, {}
    )
    assert _btc_train_hash(without_gap) != _btc_train_hash(with_gap)


def test_oos_only_gap_range_does_not_alter_train_artifact_hash():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    configs = _no_signal_configs()
    sidecars = _sidecars_with_rate(fold, 0.0001)
    oos_gap = dict.fromkeys(
        _SYMBOLS, ((fold.oos_start_ms + 500, fold.oos_start_ms + 600),)
    )

    baseline = _evaluate_fold_train(
        "S1", configs, bars_1m, sidecars, _no_gaps(), fold, {}, {}
    )
    with_oos_gap = _evaluate_fold_train(
        "S1", configs, bars_1m, sidecars, oos_gap, fold, {}, {}
    )
    assert _btc_train_hash(baseline) == _btc_train_hash(with_oos_gap)


def test_nan_funding_rate_fails_closed_not_an_unaccounted_exception():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    configs = _no_signal_configs()

    baseline = _evaluate_fold_train(
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, 0.0001),
        _no_gaps(),
        fold,
        {},
        {},
    )
    nan_candidates = _evaluate_fold_train(  # must not raise
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, float("nan")),
        _no_gaps(),
        fold,
        {},
        {},
    )
    # The NaN is folded into the hash via a stable sentinel -- distinct from
    # the finite baseline, never silently collapsed/ignored.
    assert _btc_train_hash(baseline) != _btc_train_hash(nan_candidates)


def test_generate_signals_failure_hash_binds_train_relevant_input_never_raw_secret_text():
    """Captain follow-up (2026-07-17): a bare identity+status+reason
    sentinel for a generate_signals failure means two DIFFERENT train
    bars/funding/gaps that happen to hit the SAME generator failure class
    would still collide -- the static_input_hash must now be bound in too,
    so train-relevant input changes still diverge the failure artifact hash,
    while the raw (potentially secret-bearing) exception text itself must
    NEVER leak into -- or even influence -- that hash."""
    fold = _FOLD_0
    bars_1m_a = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    bars_1m_b = {
        **bars_1m_a,
        "BTCUSDT": _flat_bars(
            fold.train_start_ms, fold.oos_end_ms, overrides={fold.train_start_ms: 999.0}
        ),
    }
    sidecars = _sidecars_with_rate(fold, 0.0001)

    def _boom_a(symbol, bars_slice, fold_id):
        raise ValueError("SECRET-A-token")

    def _boom_b(symbol, bars_slice, fold_id):
        raise ValueError("SECRET-B-completely-different-token")

    configs_a = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_boom_a) for i in range(12)
    ]
    configs_b = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_boom_b) for i in range(12)
    ]

    # Same train-relevant input, DIFFERENT raw secret exception text -> the
    # failure artifact hash must be IDENTICAL (raw text never leaks in).
    same_input_1 = _evaluate_fold_train(
        "S1", configs_a, bars_1m_a, sidecars, _no_gaps(), fold, {}, {}
    )
    same_input_2 = _evaluate_fold_train(
        "S1", configs_b, bars_1m_a, sidecars, _no_gaps(), fold, {}, {}
    )
    assert _btc_train_hash(same_input_1) == _btc_train_hash(same_input_2)

    # DIFFERENT train-relevant input, SAME generator failure class -> the
    # failure artifact hash must now DIFFER (previously collided).
    diff_input = _evaluate_fold_train(
        "S1", configs_a, bars_1m_b, sidecars, _no_gaps(), fold, {}, {}
    )
    assert _btc_train_hash(same_input_1) != _btc_train_hash(diff_input)


@pytest.mark.parametrize("rate", [float("nan"), float("inf"), float("-inf")])
def test_json_safe_funding_rate_maps_nonfinite_to_stable_string_sentinels(rate):
    assert isinstance(_json_safe_funding_rate(rate), str)


def test_json_safe_funding_rate_passes_finite_values_through_unchanged():
    assert _json_safe_funding_rate(0.0001) == 0.0001


def test_static_train_fingerprint_computed_once_per_symbol_not_once_per_config(
    monkeypatch,
):
    """Captain performance correction: bar/funding/gap hashing must be
    computed ONCE per (fold, symbol) -- 4 times per fold for the frozen
    4-symbol universe -- never once per (config, symbol) (48 times per fold
    for 12 configs), which made a naive per-config recomputation
    prohibitive for real 120-day 1m slices.

    Captain follow-up (2026-07-17): no production-global mutable call
    counter -- this test wraps ``rob944_walkforward._train_static_fingerprint``
    with a local monkeypatch spy that counts calls while delegating to the
    real function, so runtime code carries no extra instrumentation state.
    """
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    configs = _no_signal_configs(n=12)

    calls = {"count": 0}
    real_fn = rob944_walkforward._train_static_fingerprint

    def spy(*args, **kwargs):
        calls["count"] += 1
        return real_fn(*args, **kwargs)

    monkeypatch.setattr(rob944_walkforward, "_train_static_fingerprint", spy)

    _evaluate_fold_train(
        "S1",
        configs,
        bars_1m,
        _sidecars_with_rate(fold, 0.0001),
        _no_gaps(),
        fold,
        {},
        {},
    )
    assert calls["count"] == len(_SYMBOLS) == 4


# ---------------------------------------------------------------------------
# Independent walk-forward audit (2026-07-17): canonicalize generator signal
# order before hashing/execution; direct TRAIN bar/signal mutation coverage
# (previously only funding/gap were exercised); gap ranges must hash the
# CLIPPED train-intersection, never the gap's own possibly-partly-OOS
# endpoints.
# ---------------------------------------------------------------------------


def test_reversed_generator_signal_order_yields_identical_train_hash_and_trades():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    sidecars = _sidecars_with_rate(fold, 0.0001)
    # Both timestamps strictly AFTER the funding sidecar's own calc_time
    # (train_start_ms + 1000, see _sidecars_with_rate) so neither is
    # rejected as funding_evidence_unavailable.
    ts1, ts2 = fold.train_start_ms + 60_000, fold.train_start_ms + 3 * 60_000
    sig1 = _make_signal("BTCUSDT", ts1, config_id="S1-00", fold_id=fold.fold_id)
    sig2 = _make_signal("BTCUSDT", ts2, config_id="S1-00", fold_id=fold.fold_id)

    def gen_forward(symbol, bars_slice, fold_id):
        return (sig1, sig2) if symbol == "BTCUSDT" else ()

    def gen_backward(symbol, bars_slice, fold_id):
        return (sig2, sig1) if symbol == "BTCUSDT" else ()

    candidates_forward = _evaluate_fold_train(
        "S1",
        _configs_with_target_gen(gen_forward),
        bars_1m,
        sidecars,
        _no_gaps(),
        fold,
        {},
        {},
    )
    candidates_backward = _evaluate_fold_train(
        "S1",
        _configs_with_target_gen(gen_backward),
        bars_1m,
        sidecars,
        _no_gaps(),
        fold,
        {},
        {},
    )

    assert _btc_train_hash(candidates_forward) == _btc_train_hash(candidates_backward)
    ev_f = _btc_train_evidence(candidates_forward)
    ev_b = _btc_train_evidence(candidates_backward)
    assert ev_f.completed_trades == ev_b.completed_trades == 2
    assert ev_f.net_expectancy_bps == ev_b.net_expectancy_bps


def test_direct_train_bar_mutation_changes_train_artifact_hash():
    """Only funding/gap mutation was previously exercised -- this proves the
    bar slice ITSELF (bars/prices, not funding or gaps) is bound too."""
    fold = _FOLD_0
    bars_a = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    bars_b = {
        **bars_a,
        "BTCUSDT": _flat_bars(
            fold.train_start_ms, fold.oos_end_ms, overrides={fold.train_start_ms: 123.0}
        ),
    }
    sidecars = _sidecars_with_rate(fold, 0.0001)
    configs = _no_signal_configs()

    candidates_a = _evaluate_fold_train(
        "S1", configs, bars_a, sidecars, _no_gaps(), fold, {}, {}
    )
    candidates_b = _evaluate_fold_train(
        "S1", configs, bars_b, sidecars, _no_gaps(), fold, {}, {}
    )
    assert _btc_train_hash(candidates_a) != _btc_train_hash(candidates_b)


def test_direct_generated_signal_mutation_changes_train_artifact_hash():
    """Only funding/gap mutation was previously exercised -- this proves the
    generated SIGNAL's own params are bound too (bars/funding/gaps held
    fixed, only sl_distance_bps differs)."""
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    sidecars = _sidecars_with_rate(fold, 0.0001)
    ts = fold.train_start_ms

    def gen_a(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return (_make_signal(symbol, ts, config_id="S1-00", fold_id=fold_id, sl=200.0),)

    def gen_b(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return (_make_signal(symbol, ts, config_id="S1-00", fold_id=fold_id, sl=250.0),)

    candidates_a = _evaluate_fold_train(
        "S1",
        _configs_with_target_gen(gen_a),
        bars_1m,
        sidecars,
        _no_gaps(),
        fold,
        {},
        {},
    )
    candidates_b = _evaluate_fold_train(
        "S1",
        _configs_with_target_gen(gen_b),
        bars_1m,
        sidecars,
        _no_gaps(),
        fold,
        {},
        {},
    )
    assert _btc_train_hash(candidates_a) != _btc_train_hash(candidates_b)


def test_gap_hash_clips_to_train_intersection_oos_tail_change_does_not_alter_train_hash():
    """A gap that touches the train window but extends into OOS must be
    hashed as the CLIPPED train-visible portion only -- changing ONLY the
    OOS-side tail of such a gap must never alter the TRAIN hash."""
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    sidecars = _sidecars_with_rate(fold, 0.0001)
    configs = _no_signal_configs()

    gap_end_a = fold.train_end_ms + 1000  # extends slightly into embargo/OOS-side
    gap_end_b = fold.train_end_ms + 50_000  # a materially different OOS-side tail
    gap_start = (
        fold.train_end_ms - 500
    )  # starts strictly inside train, straddles the boundary

    gap_ranges_a = dict.fromkeys(_SYMBOLS, ((gap_start, gap_end_a),))
    gap_ranges_b = dict.fromkeys(_SYMBOLS, ((gap_start, gap_end_b),))

    candidates_a = _evaluate_fold_train(
        "S1", configs, bars_1m, sidecars, gap_ranges_a, fold, {}, {}
    )
    candidates_b = _evaluate_fold_train(
        "S1", configs, bars_1m, sidecars, gap_ranges_b, fold, {}, {}
    )
    assert _btc_train_hash(candidates_a) == _btc_train_hash(candidates_b)


# ---------------------------------------------------------------------------
# Captain independent Fable e2e audit (2026-07-17): the funding entry gate
# runs BEFORE the gap check within one scenario invocation -- its own
# no-trade rejections (funding_evidence_unavailable/
# expected_funding_cost_above_3bps) are real, already-observed evidence and
# must survive into the gap-rejected terminal outcome, not be silently
# dropped just because the run is ALSO gap-rejected.
# ---------------------------------------------------------------------------


def test_gap_rejected_scenario_preserves_already_observed_funding_rejection_counts():
    from funding_oi_archive import FundingRow

    bars = _flat_bars(0, 4 * 60_000)  # ts 0, 60_000, 120_000, 180_000
    sidecar = FundingSidecar.from_rows(
        "BTCUSDT",
        [
            FundingRow(
                calc_time=90_000, funding_interval_hours=8, last_funding_rate=0.0001
            )
        ],
    )
    sig_funding_rejected = (
        _make_signal(  # entry at ts=0, BEFORE the only known funding row -> rejected
            "BTCUSDT",
            0,
            config_id="S1-00",
            fold_id="fold-00",
            timeout_bars=1,
        )
    )
    sig_gap_touching = (
        _make_signal(  # entry at ts=120_000, funding-eligible -> produces a trade
            "BTCUSDT",
            120_000,
            config_id="S1-00",
            fold_id="fold-00",
            timeout_bars=1,
        )
    )
    gap_ranges = (
        (150_000, 151_000),
    )  # inside the second signal's [120_000, 180_000) trade window

    outcome, filtered = _run_scenario(
        bars,
        (sig_funding_rejected, sig_gap_touching),
        COST_SCENARIO_PRIMARY_STRESS,
        sidecar,
        gap_ranges,
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
    )

    assert outcome.status == "rejected"
    assert outcome.error_reason == REASON_DATA_GAP_IN_POSITION
    assert filtered is None
    assert outcome.no_trade_reason_counts.get("funding_evidence_unavailable") == 1


# ---------------------------------------------------------------------------
# Fable condition 1 (2026-07-17): a generator's OWN pre-execution rejection
# evidence (H3 S2's target_direction_invalid/tp_above_max/tp_below_r_min_sl/
# tp_below_abs_floor/confirmation_failed/next_bar_unavailable) must be
# validated fail-closed, canonicalized before hashing/merging, bound into
# the train input hash, and merged into EVERY independent scenario result
# (completed AND gap/crash terminal evidence) -- never silently dropped.
# ---------------------------------------------------------------------------


def _rejection(
    reason="target_direction_invalid",
    signal_ts=0,
    symbol="BTCUSDT",
    config_id="S1-00",
    strategy="S1",
    side="long",
    fold_id="fold-00",
):
    return NoTradeRecord(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        side=side,
        signal_ts=signal_ts,
        reason=reason,
        fold_id=fold_id,
    )


def _valid_window_kwargs():
    return {
        "strategy": "S1",
        "config_id": "S1-00",
        "symbol": "BTCUSDT",
        "fold_id": "fold-00",
        "window_start_ms": 0,
        "window_end_ms": 10 * 60_000,
    }


def test_validate_generated_rejections_accepts_empty():
    assert _validate_generated_rejections((), **_valid_window_kwargs()) == ()


def test_validate_generated_rejections_rejects_non_no_trade_record():
    with pytest.raises(ForgedSignalError):
        _validate_generated_rejections(("not-a-record",), **_valid_window_kwargs())


def test_validate_generated_rejections_rejects_invalid_side():
    bad = _rejection(side="sideways")
    with pytest.raises(ForgedSignalError):
        _validate_generated_rejections((bad,), **_valid_window_kwargs())


def test_validate_generated_rejections_rejects_unknown_reason():
    bad = _rejection(reason="SECRET-UNKNOWN-REASON")
    with pytest.raises(ForgedSignalError) as exc_info:
        _validate_generated_rejections((bad,), **_valid_window_kwargs())
    assert "SECRET-UNKNOWN-REASON" not in str(exc_info.value)


def test_validate_generated_rejections_rejects_forged_identity():
    bad = _rejection(config_id="S1-99")  # requested config_id is "S1-00"
    with pytest.raises(ForgedSignalError):
        _validate_generated_rejections((bad,), **_valid_window_kwargs())


def test_validate_generated_rejections_rejects_out_of_window_signal_ts():
    bad = _rejection(signal_ts=999_999_999)
    with pytest.raises(ForgedSignalError):
        _validate_generated_rejections((bad,), **_valid_window_kwargs())


def test_validate_generated_rejections_rejects_bool_signal_ts():
    bad = _rejection(
        signal_ts=True
    )  # bool is an int subclass -- must still be rejected
    with pytest.raises(ForgedSignalError):
        _validate_generated_rejections((bad,), **_valid_window_kwargs())


def test_validate_generated_rejections_rejects_duplicate_signal_ts():
    dup_a = _rejection(signal_ts=60_000, reason="target_direction_invalid")
    dup_b = _rejection(signal_ts=60_000, reason="tp_above_max")
    with pytest.raises(ForgedSignalError):
        _validate_generated_rejections((dup_a, dup_b), **_valid_window_kwargs())


def test_validate_generated_rejections_returns_canonical_sorted_order():
    later = _rejection(signal_ts=120_000, reason="tp_above_max")
    earlier = _rejection(signal_ts=60_000, reason="target_direction_invalid")
    ordered = _validate_generated_rejections((later, earlier), **_valid_window_kwargs())
    assert [r.signal_ts for r in ordered] == [60_000, 120_000]


def test_rejections_reorder_does_not_change_the_combined_hash():
    rej_a = _rejection(signal_ts=60_000, reason="target_direction_invalid")
    rej_b = _rejection(signal_ts=120_000, reason="tp_above_max")
    kwargs = _valid_window_kwargs()
    ordered_1 = _validate_generated_rejections((rej_a, rej_b), **kwargs)
    ordered_2 = _validate_generated_rejections((rej_b, rej_a), **kwargs)
    hash_1 = _combine_static_and_signals("static", (), ordered_1)
    hash_2 = _combine_static_and_signals("static", (), ordered_2)
    assert hash_1 == hash_2


def test_rejections_content_change_alters_the_combined_hash():
    rej_a = _rejection(signal_ts=60_000, reason="target_direction_invalid")
    rej_b = _rejection(signal_ts=60_000, reason="tp_above_max")
    hash_a = _combine_static_and_signals("static", (), (rej_a,))
    hash_b = _combine_static_and_signals("static", (), (rej_b,))
    assert hash_a != hash_b


def test_direct_signal_rejection_ts_collision_is_rejected():
    """Captain GeneratedSignalBatch fail-closed seam (2026-07-17): an
    accepted signal and a rejection sharing the SAME signal_ts must fail
    closed -- a forged/buggy callback could otherwise create a trade AND
    contribute a no-trade-reason count at the same timestamp."""
    sig = _make_signal("BTCUSDT", 60_000, config_id="S1-00", fold_id="fold-00")
    rejection = _rejection(
        signal_ts=60_000, reason="target_direction_invalid", fold_id="fold-00"
    )
    with pytest.raises(ForgedSignalError):
        rob944_walkforward._assert_no_signal_rejection_ts_collision(
            (sig,),
            (rejection,),
            strategy="S1",
            config_id="S1-00",
            symbol="BTCUSDT",
            fold_id="fold-00",
        )


def test_direct_signal_rejection_no_collision_when_ts_differ():
    sig = _make_signal("BTCUSDT", 60_000, config_id="S1-00", fold_id="fold-00")
    rejection = _rejection(
        signal_ts=120_000, reason="target_direction_invalid", fold_id="fold-00"
    )
    rob944_walkforward._assert_no_signal_rejection_ts_collision(  # must not raise
        (sig,),
        (rejection,),
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
    )


def test_train_signal_rejection_ts_collision_yields_zero_partial_evidence_whole_attempt_crashed():
    """A colliding generator output must crash the WHOLE symbol's TRAIN
    evidence for that config -- never partially accept the signal while
    silently dropping/keeping the colliding rejection (or vice versa)."""
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    sidecar = _sidecars_with_rate(fold, 0.0001)
    ts = fold.train_start_ms + 60_000
    colliding_sig = _make_signal("BTCUSDT", ts, config_id="S1-00", fold_id=fold.fold_id)
    colliding_rejection = _rejection(
        signal_ts=ts, reason="target_direction_invalid", fold_id=fold.fold_id
    )

    def gen(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return GeneratedSignalBatch(
            signals=(colliding_sig,), rejections=(colliding_rejection,)
        )

    crash_notes: dict = {}
    candidates = _evaluate_fold_train(
        "S1",
        _configs_with_target_gen(gen),
        bars_1m,
        sidecar,
        _no_gaps(),
        fold,
        crash_notes,
        {},
    )
    btc_evidence = _btc_train_evidence(candidates)
    # Crashed -- zero partial evidence (no completed_trades, no leaked counts
    # from either the accepted-but-forbidden signal or the rejection).
    assert btc_evidence.completed_trades == 0
    assert "S1-00" in crash_notes


def test_oos_signal_rejection_ts_collision_yields_crashed_terminal_evidence_not_partial():
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.oos_start_ms, fold.oos_end_ms)
    sidecar = _permissive_funding_sidecars()
    ts = fold.oos_start_ms
    colliding_sig = _make_signal("BTCUSDT", ts, config_id="S1-00", fold_id=fold.fold_id)
    colliding_rejection = _rejection(
        signal_ts=ts, reason="target_direction_invalid", fold_id=fold.fold_id
    )

    def gen(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return GeneratedSignalBatch(
            signals=(colliding_sig,), rejections=(colliding_rejection,)
        )

    outcomes, ledgers = _evaluate_fold_oos(
        "S1",
        ConfigSpec(config_id="S1-00", generate_signals=gen),
        bars_1m,
        sidecar,
        _no_gaps(),
        fold,
        {},
        {},
    )
    btc_crashed = [o for o in outcomes if o.status == "crashed"]
    assert (
        len(btc_crashed) == 3
    )  # all 3 independent scenarios crash, none partially succeed
    assert all(len(v) == 0 for v in ledgers.values())


def test_pre_execution_rejections_are_merged_into_a_completed_scenario_outcome():
    bars = _flat_bars(0, 2 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]
    rejection = _rejection(signal_ts=0, reason="target_direction_invalid")

    outcome, filtered = _run_scenario(
        bars,
        (),
        COST_SCENARIOS[0],
        sidecar,
        (),
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
        pre_execution_rejections=(rejection,),
    )
    assert outcome.status == "completed"
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1
    assert filtered is not None
    assert any(nt.reason == "target_direction_invalid" for nt in filtered.no_trades)


def test_pre_execution_rejections_survive_a_gap_rejected_scenario_outcome():
    """Mirrors the funding-rejection-preserved-through-gap-rejection fix --
    the generator's OWN pre-execution rejections must ALSO survive when the
    run is separately gap-rejected."""
    bars = _flat_bars(0, 4 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]
    sig_gap_touching = _make_signal(
        "BTCUSDT", 120_000, config_id="S1-00", fold_id="fold-00", timeout_bars=1
    )
    rejection = _rejection(signal_ts=0, reason="target_direction_invalid")
    gap_ranges = ((150_000, 151_000),)

    outcome, filtered = _run_scenario(
        bars,
        (sig_gap_touching,),
        COST_SCENARIO_PRIMARY_STRESS,
        sidecar,
        gap_ranges,
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
        pre_execution_rejections=(rejection,),
    )
    assert outcome.status == "rejected"
    assert filtered is None
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1


def test_normalized_batch_backward_compat_bare_tuple_generator_has_no_rejections():
    """Every existing S1/test-fixture generator returns a bare
    tuple[SignalEvent, ...] -- GeneratedSignalBatch normalization must keep
    that working unchanged, with an empty rejections tuple."""
    from rob944_walkforward import _normalize_generated_batch

    sig = _make_signal("BTCUSDT", 0, config_id="S1-00", fold_id="fold-00")
    batch = _normalize_generated_batch((sig,))
    assert batch.signals == (sig,)
    assert batch.rejections == ()


def test_normalized_batch_passes_through_a_real_generated_signal_batch_unchanged():
    """A real H3 adapter (e.g. the S2 factory) returns a GeneratedSignalBatch
    directly -- normalization must pass it through as-is, never re-wrap or
    drop its rejections."""
    from rob944_walkforward import _normalize_generated_batch

    sig = _make_signal("BTCUSDT", 0, config_id="S1-00", fold_id="fold-00")
    rejection = _rejection(signal_ts=60_000)
    batch_in = GeneratedSignalBatch(signals=(sig,), rejections=(rejection,))
    batch_out = _normalize_generated_batch(batch_in)
    assert batch_out is batch_in
    assert batch_out.rejections == (rejection,)


def test_real_generate_s2_signals_target_direction_invalid_survives_adapter_to_h4_scenario_evidence():
    """Fable condition 1 end-to-end, using the ACTUAL real generator (not a
    manually-constructed RejectedCandidate): reuses
    test_rob940_signal_s2.py's own real-pipeline fixture
    (``test_real_pipeline_target_direction_invalid_reason_count``) -- a
    genuine confirmed shock whose target lands on the WRONG side of entry
    for a long, so H3's real ``_evaluate_target_gates`` gate itself
    produces the rejection, not a hand-built stand-in. Drives it through
    the REAL run_rob944_campaign adapter conversion, REAL H4
    _validate_generated_rejections, and a REAL _run_scenario call."""
    import run_rob944_campaign as cli
    from rob940_signal_manifest import get_s2_config
    from rob940_signal_s2 import count_rejection_reasons, generate_s2_signals
    from test_rob940_signal_s2 import _zigzag_then_shock

    cfg = get_s2_config("S2-00")
    bars_5m = _zigzag_then_shock(
        shock_close=99.9, confirm_close=100.2, confirm_high=100.25, confirm_low=99.95
    )
    # E=101.0 puts E ABOVE T=100.6 for a long -> direction-invalid, even
    # though the shock/confirmation themselves are perfectly valid.
    bars_1m_fixture = [
        Bar1m(
            ts=bars_5m[-1].close_ts,
            open=101.0,
            high=101.1,
            low=100.9,
            close=101.05,
            volume=10.0,
        )
    ]
    result = generate_s2_signals(
        bars_5m, bars_1m_fixture, cfg, symbol="XRPUSDT", fold_id="fold-00"
    )
    assert result.signals == ()
    assert count_rejection_reasons(result.rejections) == {"target_direction_invalid": 1}

    # REAL adapter conversion (the exact function _s2_gen_factory calls).
    converted = cli._s2_rejections_to_no_trade_records(result.rejections)
    assert len(converted) == 1
    assert converted[0].reason == "target_direction_invalid"
    assert converted[0].strategy == "S2"
    assert converted[0].config_id == "S2-00"
    assert converted[0].symbol == "XRPUSDT"

    # REAL H4 identity/window validation, canonical order.
    window_ms = bars_1m_fixture[0].ts
    validated = _validate_generated_rejections(
        converted,
        strategy="S2",
        config_id="S2-00",
        symbol="XRPUSDT",
        fold_id="fold-00",
        window_start_ms=window_ms,
        window_end_ms=window_ms,
    )
    assert validated[0].reason == "target_direction_invalid"

    # REAL _run_scenario call: the rejection must survive into the
    # completed scenario's own no_trade_reason_counts.
    outcome, filtered = _run_scenario(
        tuple(bars_1m_fixture),
        (),
        COST_SCENARIOS[0],
        _permissive_funding_sidecars()["XRPUSDT"],
        (),
        strategy="S2",
        config_id="S2-00",
        symbol="XRPUSDT",
        fold_id="fold-00",
        pre_execution_rejections=validated,
    )
    assert outcome.status == "completed"
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1


# ---------------------------------------------------------------------------
# Captain P1 (2026-07-17): a crash/timeout at EITHER stage of _run_scenario
# must still preserve whatever no-trade evidence was already known at the
# point of failure -- pre_execution_rejections survive either crash point;
# funding_rejections additionally survive an engine-stage crash (since the
# funding gate had already run by then).
# ---------------------------------------------------------------------------


def test_funding_gate_stage_crash_preserves_pre_execution_rejection_counts(monkeypatch):
    bars = _flat_bars(0, 2 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]
    rejection = _rejection(signal_ts=0, reason="target_direction_invalid")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated funding-gate crash")

    monkeypatch.setattr(rob944_walkforward, "_apply_funding_gate", _boom)

    outcome, filtered = _run_scenario(
        bars,
        (),
        COST_SCENARIOS[0],
        sidecar,
        (),
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
        pre_execution_rejections=(rejection,),
    )
    assert outcome.status == "crashed"
    assert filtered is None
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1


def test_engine_stage_crash_preserves_both_funding_and_pre_execution_rejection_counts(
    monkeypatch,
):
    bars = _flat_bars(0, 2 * 60_000)
    rejection = _rejection(signal_ts=0, reason="target_direction_invalid")
    # A signal that the funding gate itself REJECTS (entry before any known
    # funding row) -- funding_rejections is non-empty by the time the
    # (mocked) engine stage crashes.
    sidecar_no_rate = FundingSidecar.from_rows("BTCUSDT", [])
    sig_funding_rejected = _make_signal(
        "BTCUSDT", 0, config_id="S1-00", fold_id="fold-00"
    )

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated engine crash")

    monkeypatch.setattr(rob944_walkforward, "run_symbol_stream", _boom)

    outcome, filtered = _run_scenario(
        bars,
        (sig_funding_rejected,),
        COST_SCENARIOS[0],
        sidecar_no_rate,
        (),
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
        pre_execution_rejections=(rejection,),
    )
    assert outcome.status == "crashed"
    assert filtered is None
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1
    assert outcome.no_trade_reason_counts.get("funding_evidence_unavailable") == 1


def test_train_evidence_preserves_no_trade_reason_counts_on_gap_rejected_zero_evidence():
    """The TRAIN-level _zero_evidence call site for a gap-rejected outcome
    must carry outcome.no_trade_reason_counts through -- previously always
    silently defaulted to empty, dropping a generator's own
    pre_execution_rejections (e.g. target_direction_invalid) whenever the
    TRAIN window's winning candidate was ALSO gap-rejected."""
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    sidecar = _sidecars_with_rate(fold, 0.0001)

    ts = fold.train_start_ms + 60_000
    rejection_ts = (
        fold.train_start_ms
    )  # DIFFERENT ts than the accepted signal -- no cross-list collision
    rejection = _rejection(
        signal_ts=rejection_ts, reason="target_direction_invalid", fold_id=fold.fold_id
    )
    sig_gap_touching = _make_signal(
        "BTCUSDT", ts, config_id="S1-00", fold_id=fold.fold_id, timeout_bars=1
    )
    gap_ranges = dict.fromkeys(_SYMBOLS, ())
    gap_ranges["BTCUSDT"] = ((ts + 30_000, ts + 31_000),)

    def gen(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return GeneratedSignalBatch(
            signals=(sig_gap_touching,), rejections=(rejection,)
        )

    candidates = _evaluate_fold_train(
        "S1", _configs_with_target_gen(gen), bars_1m, sidecar, gap_ranges, fold, {}, {}
    )
    btc_evidence = _btc_train_evidence(candidates)
    assert btc_evidence.no_trade_reason_counts.get("target_direction_invalid") == 1


def test_funding_gate_stage_timeout_preserves_pre_execution_rejection_counts(
    monkeypatch,
):
    """TimeoutError coverage (captain follow-up) -- same preservation
    guarantee as a generic crash, but classified status="timeout"."""
    bars = _flat_bars(0, 2 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]
    rejection = _rejection(signal_ts=0, reason="target_direction_invalid")

    def _timeout(*args, **kwargs):
        raise TimeoutError("simulated funding-gate timeout")

    monkeypatch.setattr(rob944_walkforward, "_apply_funding_gate", _timeout)

    outcome, filtered = _run_scenario(
        bars,
        (),
        COST_SCENARIOS[0],
        sidecar,
        (),
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
        pre_execution_rejections=(rejection,),
    )
    assert outcome.status == "timeout"
    assert filtered is None
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1


def test_engine_stage_timeout_preserves_both_funding_and_pre_execution_rejection_counts(
    monkeypatch,
):
    bars = _flat_bars(0, 2 * 60_000)
    rejection = _rejection(signal_ts=0, reason="target_direction_invalid")
    sidecar_no_rate = FundingSidecar.from_rows("BTCUSDT", [])
    sig_funding_rejected = _make_signal(
        "BTCUSDT", 0, config_id="S1-00", fold_id="fold-00"
    )

    def _timeout(*args, **kwargs):
        raise TimeoutError("simulated engine timeout")

    monkeypatch.setattr(rob944_walkforward, "run_symbol_stream", _timeout)

    outcome, filtered = _run_scenario(
        bars,
        (sig_funding_rejected,),
        COST_SCENARIOS[0],
        sidecar_no_rate,
        (),
        strategy="S1",
        config_id="S1-00",
        symbol="BTCUSDT",
        fold_id="fold-00",
        pre_execution_rejections=(rejection,),
    )
    assert outcome.status == "timeout"
    assert filtered is None
    assert outcome.no_trade_reason_counts.get("target_direction_invalid") == 1
    assert outcome.no_trade_reason_counts.get("funding_evidence_unavailable") == 1


# ---------------------------------------------------------------------------
# ROB-970 (Q2/Q3, Fable-approved orch-fable-answer-rob970-20260719.md):
# typed, sanitized child-failure diagnostic evidence captured at the FIRST
# generator/funding-gate/engine catch, carried via an optional side-channel
# (never altering _run_scenario's/_crash_outcome's own return shape) into
# ConfigAttemptResult/ConfigAttemptEvidenceSummary -- separate from any fixed
# reason code or semantic hash.
# ---------------------------------------------------------------------------


def test_funding_gate_catch_records_typed_diagnostic_evidence():
    bars = _flat_bars(0, 2 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated funding-gate crash")

    import pytest as _pytest

    monkeypatch = _pytest.MonkeyPatch()
    monkeypatch.setattr(rob944_walkforward, "_apply_funding_gate", _boom)
    try:
        diagnostic_notes: dict[str, list[ChildFailureEvidence]] = {}
        outcome, filtered = _run_scenario(
            bars,
            (),
            COST_SCENARIOS[0],
            sidecar,
            (),
            strategy="S1",
            config_id="S1-00",
            symbol="BTCUSDT",
            fold_id="fold-00",
            diagnostic_notes=diagnostic_notes,
        )
    finally:
        monkeypatch.undo()
    assert outcome.status == "crashed"
    assert filtered is None
    entries = diagnostic_notes["S1-00"]
    assert len(entries) == 1
    evidence = entries[0]
    assert evidence.transport == "in_process"
    assert evidence.stage == "funding_gate"
    assert evidence.exception_type == "RuntimeError"
    assert "simulated funding-gate crash" in evidence.message
    assert evidence.traceback_text
    assert evidence.stderr is None
    assert evidence.strategy == "S1"
    assert evidence.config_id == "S1-00"
    assert evidence.occurrence_count == 1
    # the RAW exception text survives in the diagnostic carrier (this is the
    # whole point) but never in the fixed reason/status.
    assert outcome.error_reason is not None


def test_engine_catch_records_typed_diagnostic_evidence():
    bars = _flat_bars(0, 2 * 60_000)
    sidecar_no_rate = FundingSidecar.from_rows("BTCUSDT", [])
    sig = _make_signal("BTCUSDT", 0, config_id="S1-00", fold_id="fold-00")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated engine crash")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rob944_walkforward, "run_symbol_stream", _boom)
    try:
        diagnostic_notes: dict[str, list[ChildFailureEvidence]] = {}
        outcome, filtered = _run_scenario(
            bars,
            (sig,),
            COST_SCENARIOS[0],
            sidecar_no_rate,
            (),
            strategy="S1",
            config_id="S1-00",
            symbol="BTCUSDT",
            fold_id="fold-00",
            diagnostic_notes=diagnostic_notes,
        )
    finally:
        monkeypatch.undo()
    assert outcome.status == "crashed"
    assert filtered is None
    entries = diagnostic_notes["S1-00"]
    assert len(entries) == 1
    assert entries[0].stage == "engine"
    assert entries[0].exception_type == "RuntimeError"


def test_run_scenario_without_diagnostic_notes_kwarg_is_unaffected():
    """Backward compatibility: every existing caller (rob960_pbo_evaluator,
    the rest of this test file) omits ``diagnostic_notes`` entirely -- the
    return shape/behavior must stay byte-identical.

    R2 audit item 5 (observer-effect-0, "successful capture/no-capture
    WalkForwardResult canonical payload/hash"): the SAME simulated crash is
    also run with the diagnostic side-channel genuinely WIRED
    (``diagnostic_notes={}``, so it IS populated) and a canonical hash of
    the semantic-only fields of both outcomes is asserted byte-identical --
    not just spot-checked field by field -- while confirming the diagnostic
    channel really did differ (empty vs. populated)."""
    bars = _flat_bars(0, 2 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated crash, no diagnostic_notes passed")

    def _semantic_payload(outcome):
        return {
            "scenario_name": outcome.scenario_name,
            "status": outcome.status,
            "trade_count": outcome.trade_count,
            "artifact_hash": outcome.artifact_hash,
            "error_reason": outcome.error_reason,
            "no_trade_reason_counts": outcome.no_trade_reason_counts,
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rob944_walkforward, "_apply_funding_gate", _boom)
    try:
        no_capture, filtered = _run_scenario(
            bars,
            (),
            COST_SCENARIOS[0],
            sidecar,
            (),
            strategy="S1",
            config_id="S1-00",
            symbol="BTCUSDT",
            fold_id="fold-00",
        )
        diagnostic_notes: dict[str, list[ChildFailureEvidence]] = {}
        with_capture, filtered_with_capture = _run_scenario(
            bars,
            (),
            COST_SCENARIOS[0],
            sidecar,
            (),
            strategy="S1",
            config_id="S1-00",
            symbol="BTCUSDT",
            fold_id="fold-00",
            diagnostic_notes=diagnostic_notes,
        )
    finally:
        monkeypatch.undo()
    assert no_capture.status == "crashed"
    assert filtered is None
    assert filtered_with_capture is None
    # the diagnostic side-channel genuinely differs (not wired vs. wired-
    # and-populated) -- otherwise this wouldn't be a real observer-effect-0
    # proof.
    assert diagnostic_notes["S1-00"]
    # ...yet the outcome's own SEMANTIC canonical hash is byte-identical.
    assert canonical_sha256(_semantic_payload(no_capture)) == canonical_sha256(
        _semantic_payload(with_capture)
    )


def test_generator_catch_records_typed_diagnostic_evidence_train_and_oos():
    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError("boom: synthetic signal-generation failure")

    diagnostic_notes: dict[str, list[ChildFailureEvidence]] = {}
    _evaluate_fold_train(
        "S1",
        [ConfigSpec(config_id="S1-00", generate_signals=_raising_gen)],
        {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS},
        _permissive_funding_sidecars(),
        _no_gaps(),
        _FOLD_0,
        {},
        {},
        diagnostic_notes,
    )
    train_entries = diagnostic_notes["S1-00"]
    assert train_entries
    assert train_entries[0].stage == "generator"
    assert train_entries[0].exception_type == "RuntimeError"
    assert train_entries[0].fold_id == "fold-00"

    diagnostic_notes_oos: dict[str, list[ChildFailureEvidence]] = {}
    _evaluate_fold_oos(
        "S1",
        ConfigSpec(config_id="S1-00", generate_signals=_raising_gen),
        {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS},
        _permissive_funding_sidecars(),
        _no_gaps(),
        _FOLD_0,
        {},
        {},
        diagnostic_notes_oos,
    )
    oos_entries = diagnostic_notes_oos["S1-00"]
    assert oos_entries
    assert oos_entries[0].stage == "generator"


def test_walkforward_end_to_end_crashed_attempt_carries_diagnostic_evidence():
    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError("boom: synthetic signal-generation failure")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_raising_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    assert all(a.status == "crashed" for a in result.config_attempts)
    for attempt in result.config_attempts:
        assert attempt.diagnostic_evidence
        assert all(
            isinstance(e, ChildFailureEvidence) for e in attempt.diagnostic_evidence
        )
        assert attempt.diagnostic_evidence[0].exception_type == "RuntimeError"

    summaries = summarize_config_attempts_for_h6(result)
    for summary in summaries:
        assert summary.diagnostic_evidence
        assert summary.reason_code == REASON_CHILD_EXECUTION_CRASHED  # unchanged, fixed


def test_repeated_identical_generator_failure_across_folds_dedupes_to_one_entry():
    """The SAME root cause recurring across every fold/symbol collapses to
    ONE diagnostic entry with an incrementing occurrence_count, never N
    near-duplicate rows -- Fable Q2 dedupe condition."""

    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError("boom: synthetic signal-generation failure")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_raising_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    attempt = next(a for a in result.config_attempts if a.config_id == "S1-00")
    # every symbol (4) hits the SAME generator failure at TRAIN in fold-00
    # alone -- must dedupe to exactly one signature, not one row per symbol.
    assert len(attempt.diagnostic_evidence) == 1
    assert attempt.diagnostic_evidence[0].occurrence_count > 1


def test_diagnostic_evidence_never_leaks_secret_text_that_crash_log_itself_carries():
    """crash_log intentionally keeps raw text (unchanged, existing
    behavior); the NEW diagnostic carrier must still be sanitized -- proving
    the two serve different purposes (raw internal note vs. persistable
    evidence)."""

    def _raising_gen(symbol, bars_slice, fold_id):
        raise RuntimeError("token=eyJhbGciOiJIUzI1NiJ9.leaked-secret-payload")

    specs = [
        ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_raising_gen)
        for i in range(12)
    ]
    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}
    result = run_walkforward(
        strategy="S1",
        configs=tuple(specs),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    attempt = next(a for a in result.config_attempts if a.config_id == "S1-00")
    for evidence in attempt.diagnostic_evidence:
        assert "eyJhbGciOiJIUzI1NiJ9.leaked-secret-payload" not in evidence.message
        assert (
            "eyJhbGciOiJIUzI1NiJ9.leaked-secret-payload" not in evidence.traceback_text
        )


def test_diagnostic_evidence_never_changes_fixed_reason_code_or_status():
    """Observer-effect-0: two runs whose ONLY difference is the raw
    exception message (secret-bearing vs. not) produce identical
    status/reason_code -- the diagnostic carrier can never influence
    semantic identity."""

    def _gen_a(symbol, bars_slice, fold_id):
        raise RuntimeError("SECRET-A-token")

    def _gen_b(symbol, bars_slice, fold_id):
        raise RuntimeError("totally different message, no secret")

    bars_1m = {s: _flat_bars(0, _WINDOW_END) for s in _SYMBOLS}

    result_a = run_walkforward(
        strategy="S1",
        configs=tuple(
            ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_gen_a)
            for i in range(12)
        ),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    result_b = run_walkforward(
        strategy="S1",
        configs=tuple(
            ConfigSpec(config_id=f"S1-{i:02d}", generate_signals=_gen_b)
            for i in range(12)
        ),
        bars_1m=bars_1m,
        funding_sidecars=_permissive_funding_sidecars(),
        gap_ranges=_no_gaps(),
        fold_schedule=(_FOLD_0, _FOLD_1),
    )
    summaries_a = {s.config_id: s for s in summarize_config_attempts_for_h6(result_a)}
    summaries_b = {s.config_id: s for s in summarize_config_attempts_for_h6(result_b)}
    for cid in summaries_a:
        assert summaries_a[cid].status == summaries_b[cid].status
        assert summaries_a[cid].reason_code == summaries_b[cid].reason_code
        # diagnostic content itself legitimately differs -- only semantic
        # identity is required to match.
        assert (
            summaries_a[cid].diagnostic_evidence[0].message
            != summaries_b[cid].diagnostic_evidence[0].message
        )


def test_crashed_scenario_artifact_hash_changes_when_preserved_histogram_changes():
    """The preserved no_trade_reason_counts must be genuinely BOUND into
    the crashed/timeout artifact_hash, not merely attached cosmetically."""
    bars = _flat_bars(0, 2 * 60_000)
    sidecar = _permissive_funding_sidecars()["BTCUSDT"]

    def _make_outcome(reason):
        rejection = _rejection(signal_ts=0, reason=reason)

        def _boom(*args, **kwargs):
            raise RuntimeError("simulated crash")

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as mp:
            mp.setattr(rob944_walkforward, "_apply_funding_gate", _boom)
            outcome, _filtered = _run_scenario(
                bars,
                (),
                COST_SCENARIOS[0],
                sidecar,
                (),
                strategy="S1",
                config_id="S1-00",
                symbol="BTCUSDT",
                fold_id="fold-00",
                pre_execution_rejections=(rejection,),
            )
        return outcome

    outcome_a = _make_outcome("target_direction_invalid")
    outcome_b = _make_outcome("tp_above_max")
    assert outcome_a.status == outcome_b.status == "crashed"
    assert outcome_a.artifact_hash != outcome_b.artifact_hash


def test_train_evidence_preserves_no_trade_reason_counts_on_engine_crash_zero_evidence():
    """The TRAIN-level _zero_evidence call site for a CRASHED (not just
    gap-rejected) outcome must also carry outcome.no_trade_reason_counts
    through."""
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.train_start_ms, fold.oos_end_ms)
    sidecar = _sidecars_with_rate(fold, 0.0001)

    ts = fold.train_start_ms + 60_000
    rejection_ts = (
        fold.train_start_ms
    )  # DIFFERENT ts than the accepted signal -- no cross-list collision
    rejection = _rejection(
        signal_ts=rejection_ts, reason="target_direction_invalid", fold_id=fold.fold_id
    )
    sig = _make_signal(
        "BTCUSDT", ts, config_id="S1-00", fold_id=fold.fold_id, timeout_bars=1
    )

    def gen(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return GeneratedSignalBatch(signals=(sig,), rejections=(rejection,))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated engine crash")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rob944_walkforward, "run_symbol_stream", _boom)
        candidates = _evaluate_fold_train(
            "S1",
            _configs_with_target_gen(gen),
            bars_1m,
            sidecar,
            _no_gaps(),
            fold,
            {},
            {},
        )
    btc_evidence = _btc_train_evidence(candidates)
    assert btc_evidence.no_trade_reason_counts.get("target_direction_invalid") == 1


def test_oos_preserves_target_direction_invalid_in_each_of_all_3_crashed_scenarios():
    """_evaluate_fold_oos runs 3 INDEPENDENT scenario invocations -- a
    generator's own pre_execution_rejections must survive into EACH of the
    3, not just one, when the engine crashes in every one of them."""
    fold = _FOLD_0
    bars_1m = _flat_bars_all_symbols(fold.oos_start_ms, fold.oos_end_ms)
    sidecar = _permissive_funding_sidecars()
    ts = fold.oos_start_ms
    rejection = _rejection(
        signal_ts=ts, reason="target_direction_invalid", fold_id=fold.fold_id
    )

    def gen(symbol, bars_slice, fold_id):
        if symbol != "BTCUSDT":
            return ()
        return GeneratedSignalBatch(signals=(), rejections=(rejection,))

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated engine crash")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(rob944_walkforward, "run_symbol_stream", _boom)
        outcomes, _ledgers = _evaluate_fold_oos(
            "S1",
            ConfigSpec(config_id="S1-00", generate_signals=gen),
            bars_1m,
            sidecar,
            _no_gaps(),
            fold,
            {},
            {},
        )
    btc_outcomes = [o for o in outcomes if o.status == "crashed"]
    # 4 symbols x 3 scenarios = 12 outcomes; only BTCUSDT's 3 carry the
    # rejection (other symbols produced no signals/rejections at all).
    btc_scenario_outcomes = [o for o in btc_outcomes if o.no_trade_reason_counts]
    assert len(btc_scenario_outcomes) == 3
    assert all(
        o.no_trade_reason_counts.get("target_direction_invalid") == 1
        for o in btc_scenario_outcomes
    )
