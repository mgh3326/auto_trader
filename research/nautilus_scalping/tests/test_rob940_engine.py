"""ROB-942 (H2, ROB-940) — deterministic 1m execution engine RED fixtures.

``SignalEvent.signal_ts`` is defined as the CLOSE boundary of the signal bar,
which (for a contiguous 1m grid) is numerically identical to the ``ts`` (open
time) of the very next 1m bar. The engine's "next contiguous 1m bar" is
therefore the 1m bar found AT ``ts == signal_ts``; if it's missing (a gap in
the 1m grid right at the execution boundary), there is no entry. This keeps
the engine decoupled from signal-timeframe (5m vs 15m) bucket size — see
``rob940_engine`` module docstring for the full ultrathink rationale.
"""

import rob940_cost_model as cm
from rob940_bars_agg import Bar1m
from rob940_engine import SignalEvent, resolve_entry, run_symbol_stream

MIN = 60_000


def _mk(ts0, specs):
    """specs: list of (open, high, low, close) tuples, one per 1m bar."""
    return [
        Bar1m(ts=ts0 + i * MIN, open=o, high=h, low=lo, close=c, volume=1.0)
        for i, (o, h, lo, c) in enumerate(specs)
    ]


def _sig(
    signal_ts,
    side="long",
    sl=100.0,
    tp=None,
    tp_target=None,
    timeout=10,
    cooldown=0,
    strategy="s1",
    config_id="c1",
    symbol="XRPUSDT",
    fold_id=None,
):
    kwargs = {
        "strategy": strategy,
        "config_id": config_id,
        "symbol": symbol,
        "signal_ts": signal_ts,
        "side": side,
        "sl_distance_bps": sl,
        "timeout_bars": timeout,
        "cooldown_bars": cooldown,
        "fold_id": fold_id,
    }
    if tp_target is not None:
        kwargs["tp_target_price"] = tp_target
    else:
        kwargs["tp_distance_bps"] = tp if tp is not None else 200.0
    return SignalEvent(**kwargs)


# --------------------------------------------------------------------------- #
# SignalEvent validation
# --------------------------------------------------------------------------- #
def test_signal_event_requires_exactly_one_tp_spec():
    for kw in (
        {"tp_distance_bps": 100.0, "tp_target_price": 101.0},
        {},
    ):
        try:
            SignalEvent(
                strategy="s",
                config_id="c",
                symbol="X",
                signal_ts=0,
                side="long",
                sl_distance_bps=50.0,
                **kw,
            )
            raised = False
        except ValueError:
            raised = True
        assert raised, kw


def test_signal_event_validates_positive_fields():
    base = {
        "strategy": "s",
        "config_id": "c",
        "symbol": "X",
        "signal_ts": 0,
        "side": "long",
        "tp_distance_bps": 100.0,
    }
    for bad in (
        {"sl_distance_bps": 0.0},
        {"sl_distance_bps": -1.0},
    ):
        try:
            SignalEvent(**{**base, "sl_distance_bps": 50.0, **bad})
            raised = False
        except ValueError:
            raised = True
        assert raised
    try:
        SignalEvent(**base, sl_distance_bps=50.0, timeout_bars=0)
        raised = False
    except ValueError:
        raised = True
    assert raised
    try:
        SignalEvent(**base, sl_distance_bps=50.0, cooldown_bars=-1)
        raised = False
    except ValueError:
        raised = True
    assert raised
    try:
        SignalEvent(**{**base, "sl_distance_bps": 50.0, "side": "sideways"})
        raised = False
    except ValueError:
        raised = True
    assert raised


# --------------------------------------------------------------------------- #
# next-1m-open entry + non-contiguous no-entry (AC3, AC10)
# --------------------------------------------------------------------------- #
def test_resolve_entry_returns_bar_index_at_signal_ts():
    bars = _mk(0, [(10, 10, 10, 10), (105, 110, 100, 108), (108, 108, 108, 108)])
    assert resolve_entry(bars, signal_ts=MIN) == 1


def test_resolve_entry_none_when_bar_missing_at_signal_ts():
    bars = _mk(0, [(10, 10, 10, 10)]) + _mk(2 * MIN, [(20, 20, 20, 20)])  # gap at MIN
    assert resolve_entry(bars, signal_ts=MIN) is None


def test_next_1m_open_used_as_entry_price_not_signal_bar_close():
    # bar at signal_ts has open=105 but a DIFFERENT close=108 -- entry must be 105.
    bars = _mk(
        0, [(10, 10, 10, 10), (105, 110, 100, 108)] + [(108, 109, 107, 108)] * 20
    )
    sig = _sig(signal_ts=MIN, tp=300.0, timeout=15)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result.trades) == 1
    assert result.trades[0].entry_price == 105.0
    assert result.trades[0].entry_ts == MIN


def test_non_contiguous_no_entry_produces_no_trade_record():
    bars = _mk(0, [(10, 10, 10, 10)]) + _mk(
        2 * MIN, [(20, 20, 20, 20)] * 5
    )  # gap at 1*MIN
    sig = _sig(signal_ts=MIN)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert result.trades == ()
    assert len(result.no_trades) == 1
    assert result.no_trades[0].reason == "next_bar_unavailable"


# --------------------------------------------------------------------------- #
# lookahead sentinel
# --------------------------------------------------------------------------- #
def test_walk_exits_on_first_qualifying_bar_not_a_later_more_favorable_one():
    # entry E=100, SL=99 (100bps), TP=110 (1000bps, i.e. deliberately far).
    # bar[entry+1] touches SL; bar[entry+5] would touch TP if peeked -- must not.
    specs = [(100, 100, 100, 100)]  # entry bar itself, flat
    specs += [(99.5, 99.6, 98.5, 99.0)]  # entry+1: touches SL (low<=99)
    specs += [(99.0, 99.2, 98.8, 99.0)] * 3  # entry+2..4: irrelevant
    specs += [(99.0, 120.0, 98.5, 110.0)]  # entry+5: would touch TP if peeked
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, sl=100.0, tp=1000.0, timeout=10)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_ts == 1 * MIN
    assert t.exit_price == 99.0


def test_truncating_data_after_the_actual_exit_bar_does_not_change_the_trade():
    specs = [(100, 100, 100, 100), (99.5, 99.6, 98.5, 99.0)]
    specs_extra = (
        specs + [(200, 300, 190, 250)] * 5
    )  # wild future data, must not matter
    bars_full = _mk(0, specs_extra)
    bars_trunc = _mk(0, specs)
    sig = _sig(signal_ts=0, sl=100.0, tp=1000.0, timeout=10)
    r_full = run_symbol_stream(bars_full, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    r_trunc = run_symbol_stream(bars_trunc, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert r_full.trades[0] == r_trunc.trades[0]


# --------------------------------------------------------------------------- #
# long/short symmetry
# --------------------------------------------------------------------------- #
def test_long_short_symmetry_mirrored_stop_loss():
    long_specs = [(100, 100, 100, 100), (99.5, 99.6, 98.5, 99.0)]
    short_specs = [(100, 100, 100, 100), (100.5, 101.5, 100.4, 101.0)]
    bars_long = _mk(0, long_specs)
    bars_short = _mk(0, short_specs)
    sig_long = _sig(signal_ts=0, side="long", sl=100.0, tp=1000.0)
    sig_short = _sig(signal_ts=0, side="short", sl=100.0, tp=1000.0)
    r_long = run_symbol_stream(bars_long, [sig_long], cm.COST_SCENARIO_PRIMARY_STRESS)
    r_short = run_symbol_stream(
        bars_short, [sig_short], cm.COST_SCENARIO_PRIMARY_STRESS
    )
    tl, ts = r_long.trades[0], r_short.trades[0]
    assert tl.exit_reason == ts.exit_reason == "stop_loss"
    assert abs(tl.gross_bps - ts.gross_bps) < 1e-6
    assert abs(tl.net_bps - ts.net_bps) < 1e-6


# --------------------------------------------------------------------------- #
# gap-through vs normal touch, same-bar SL-first (AC5)
# --------------------------------------------------------------------------- #
def test_gap_through_sl_fills_at_unfavorable_open_long():
    specs = [(100, 100, 100, 100), (98.0, 98.5, 97.0, 98.2)]  # gaps below SL=99
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=100.0, tp=1000.0)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == 98.0  # bar's open, NOT sl_price=99.0
    assert t.gap_fill is True


def test_gap_through_tp_fills_at_barrier_not_open_long():
    specs = [(100, 100, 100, 100), (103.0, 104.0, 102.5, 103.5)]  # gaps above TP=102
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=1000.0, tp=200.0)  # TP = 102
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "take_profit"
    assert t.exit_price == 102.0  # TP barrier, NOT the more favorable open=103
    assert t.gap_fill is False


def test_normal_intrabar_touch_fills_at_exact_barrier_not_extreme():
    specs = [
        (100, 100, 100, 100),
        (100.5, 102.5, 99.8, 101.0),
    ]  # touches TP=102 intrabar
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=1000.0, tp=200.0)  # TP=102
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "take_profit"
    assert t.exit_price == 102.0  # barrier, not intrabar high=102.5
    assert t.gap_fill is False


def test_same_bar_both_touched_sl_wins_long():
    specs = [
        (100, 100, 100, 100),
        (100.2, 103.0, 98.0, 101.0),
    ]  # touches both SL=99,TP=102
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=100.0, tp=200.0)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == 99.0


def test_same_bar_both_touched_sl_wins_short():
    specs = [
        (100, 100, 100, 100),
        (99.8, 102.0, 97.0, 99.0),
    ]  # SL=101(short), TP=98(short)
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="short", sl=100.0, tp=200.0)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == 101.0


# --------------------------------------------------------------------------- #
# timeout (AC5, AC8)
# --------------------------------------------------------------------------- #
def test_timeout_exits_at_first_deadline_bar_open():
    # SL/TP deliberately unreachable; timeout_bars=3 -> deadline at entry_idx+3
    specs = [(100, 100, 100, 100)]
    specs += [(100, 100.2, 99.8, 100)] * 2  # entry+1, entry+2: flat, no touch
    specs += [(101.5, 101.6, 101.4, 101.5)]  # entry+3: deadline bar
    specs += [(999, 999, 999, 999)]  # must never be reached
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=1000.0, tp=1000.0, timeout=3)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "timeout"
    assert t.exit_ts == 3 * MIN
    assert t.exit_price == 101.5


def test_deadline_bar_gap_through_sl_still_classified_stop_loss_not_timeout():
    specs = [(100, 100, 100, 100)]
    specs += [(100, 100.2, 99.8, 100)] * 1  # entry+1
    specs += [(97.0, 97.2, 96.5, 97.0)]  # entry+2: deadline bar, gaps through SL=99
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=100.0, tp=1000.0, timeout=2)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "stop_loss"
    assert t.exit_price == 97.0
    assert t.gap_fill is True


def test_run_off_end_of_data_before_timeout_closes_at_last_close():
    specs = [(100, 100, 100, 100), (100, 100.2, 99.8, 100.1)]
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, side="long", sl=1000.0, tp=1000.0, timeout=50)
    result = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    t = result.trades[0]
    assert t.exit_reason == "timeout"
    assert t.exit_ts == 1 * MIN
    assert t.exit_price == 100.1


# --------------------------------------------------------------------------- #
# cooldown / single-position-per-symbol (AC4)
# --------------------------------------------------------------------------- #
def test_cooldown_blocks_entry_before_threshold_and_allows_at_threshold():
    # trade 1: entry at idx0, TP hit at idx1 (exit_idx=1). cooldown=3 -> earliest allowed=4.
    specs = [
        (100, 100, 100, 100),
        (100.5, 102.0, 100.4, 101.9),
    ]  # idx0 entry, idx1 TP exit
    specs += [(101, 101, 101, 101)] * 6  # idx2..7 filler, flat (never touches anything)
    bars = _mk(0, specs)
    sig1 = _sig(signal_ts=0, side="long", sl=1000.0, tp=200.0, timeout=1, cooldown=3)
    sig_too_early = _sig(
        signal_ts=3 * MIN, side="long", sl=1000.0, tp=1000.0, timeout=1
    )  # idx3 < 4
    sig_ok = _sig(
        signal_ts=4 * MIN, side="long", sl=1000.0, tp=1000.0, timeout=1
    )  # idx4 == 4
    result = run_symbol_stream(
        bars, [sig1, sig_too_early, sig_ok], cm.COST_SCENARIO_PRIMARY_STRESS
    )
    assert len(result.trades) == 2
    reasons = [nt.reason for nt in result.no_trades]
    assert "cooldown_active" in reasons


def test_overlapping_signal_during_open_hold_is_rejected_via_cooldown_check():
    specs = [(100, 100, 100, 100)]
    specs += [
        (100, 100.2, 99.8, 100)
    ] * 5  # flat, no touch; timeout=5 keeps position open
    bars = _mk(0, specs)
    sig1 = _sig(signal_ts=0, side="long", sl=1000.0, tp=1000.0, timeout=5, cooldown=0)
    sig_overlap = _sig(signal_ts=2 * MIN, side="long", sl=1000.0, tp=1000.0, timeout=1)
    result = run_symbol_stream(
        bars, [sig1, sig_overlap], cm.COST_SCENARIO_PRIMARY_STRESS
    )
    assert len(result.trades) == 1
    assert result.no_trades[0].reason == "cooldown_active"


# --------------------------------------------------------------------------- #
# daily caps / stops (AC8)
# --------------------------------------------------------------------------- #
def _quick_tp_trade_specs():
    # 1-bar hold, TP always hit on bar 1 (open=100.5, high=102 >= TP 102)
    return [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]


def test_daily_entry_cap_blocks_fourth_entry_same_utc_day():
    block = _quick_tp_trade_specs()
    specs = block * 5  # each pair: entry bar + TP-exit bar, back to back, no gap
    bars = _mk(0, specs)
    sigs = [
        _sig(signal_ts=2 * i * MIN, side="long", sl=1000.0, tp=200.0, timeout=1)
        for i in range(5)
    ]
    result = run_symbol_stream(bars, sigs, cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result.trades) == 3
    cap_reasons = [nt.reason for nt in result.no_trades]
    assert cap_reasons.count("daily_entry_cap") == 2


def test_consecutive_two_stop_outs_halts_day():
    sl_specs = [(100, 100, 100, 100), (99.0, 99.1, 98.0, 98.5)]  # SL=99 touched
    specs = sl_specs + sl_specs  # two consecutive SL trades, back to back
    specs += [
        (100, 100, 100, 100),
        (100.5, 102.0, 100.4, 101.9),
    ]  # a 3rd (would-be TP) trade
    bars = _mk(0, specs)
    sigs = [
        _sig(signal_ts=0, side="long", sl=100.0, tp=200.0, timeout=1),
        _sig(signal_ts=2 * MIN, side="long", sl=100.0, tp=200.0, timeout=1),
        _sig(signal_ts=4 * MIN, side="long", sl=1000.0, tp=200.0, timeout=1),
    ]
    result = run_symbol_stream(bars, sigs, cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result.trades) == 2
    assert all(t.exit_reason == "stop_loss" for t in result.trades)
    assert result.no_trades[0].reason == "daily_stop_active"


def test_single_trade_minus_2r_halts_day():
    # tight SL (10bps) so a single stop-out's cost-included R <= -2.0 alone.
    specs = [
        (100, 100, 100, 100),
        (99.9, 99.91, 99.89, 99.9),
    ]  # SL=99.9 (10bps) touched
    specs += [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]  # would-be TP trade
    bars = _mk(0, specs)
    sigs = [
        _sig(signal_ts=0, side="long", sl=10.0, tp=200.0, timeout=1),
        _sig(signal_ts=2 * MIN, side="long", sl=1000.0, tp=200.0, timeout=1),
    ]
    result = run_symbol_stream(bars, sigs, cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result.trades) == 1
    t = result.trades[0]
    r = t.net_bps / 10.0
    assert r <= -2.0
    assert result.no_trades[0].reason == "daily_stop_active"


def test_daily_state_resets_on_a_new_utc_day():
    day1 = [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]
    bars_day1 = _mk(0, day1 * 3)  # 3 entries on day 0, exhausts cap
    day2_start = 24 * 60 * MIN  # +1 day in ms, aligned to a 1m boundary
    bars_day2 = _mk(day2_start, day1)
    bars = bars_day1 + bars_day2
    sigs = [
        _sig(signal_ts=2 * i * MIN, side="long", sl=1000.0, tp=200.0, timeout=1)
        for i in range(3)
    ]
    sigs.append(_sig(signal_ts=day2_start, side="long", sl=1000.0, tp=200.0, timeout=1))
    result = run_symbol_stream(bars, sigs, cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result.trades) == 4  # day-2 entry not blocked by day-1's exhausted cap
    assert result.no_trades == ()


# --------------------------------------------------------------------------- #
# 68bp boundary (AC7)
# --------------------------------------------------------------------------- #
def test_68bp_exact_passes_and_67_99_fails():
    specs = [(100, 100, 100, 100), (100, 100.1, 99.9, 100)]
    bars = _mk(0, specs)
    sig_pass = _sig(signal_ts=0, tp=68.0, sl=100.0, timeout=1)
    result_pass = run_symbol_stream(bars, [sig_pass], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result_pass.trades) == 1

    sig_fail = _sig(signal_ts=0, tp=67.99, sl=100.0, timeout=1)
    result_fail = run_symbol_stream(bars, [sig_fail], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert result_fail.trades == ()
    assert result_fail.no_trades[0].reason == "tp_below_min_distance"


def test_68bp_gate_applies_to_absolute_target_price_too():
    bars = _mk(0, [(100, 100, 100, 100), (100, 100.1, 99.9, 100)])
    # entry=100 -> target=100.68 is exactly 68bps
    sig_pass = _sig(signal_ts=0, sl=100.0, tp_target=100.68, timeout=1)
    result_pass = run_symbol_stream(bars, [sig_pass], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert len(result_pass.trades) == 1

    sig_fail = _sig(signal_ts=0, sl=100.0, tp_target=100.6799, timeout=1)
    result_fail = run_symbol_stream(bars, [sig_fail], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert result_fail.trades == ()
    assert result_fail.no_trades[0].reason == "tp_below_min_distance"


# --------------------------------------------------------------------------- #
# 13/17/22bp cost scenarios + fee/funding no-double-count (AC6)
# --------------------------------------------------------------------------- #
def test_13_17_22_cost_scenarios_share_trade_path_differ_only_in_net():
    # NOTE (ROB-942 R1 correction): this pins the narrow case where the
    # single trade never triggers AC8's cost-included daily stop, so all
    # three scenarios' independent runs happen to walk the same path. It is
    # NOT a general "scenarios always share a path" guarantee -- see
    # test_cost_scenario_dependent_daily_stop_diverges_trade_count below for
    # the case where they provably do not.
    specs = [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, sl=1000.0, tp=200.0, timeout=1)
    results = {
        s.name: run_symbol_stream(bars, [sig], s).trades[0] for s in cm.COST_SCENARIOS
    }
    base, primary, upward = (
        results["base"],
        results["primary_stress"],
        results["upward_stress"],
    )
    assert base.entry_ts == primary.entry_ts == upward.entry_ts
    assert base.exit_ts == primary.exit_ts == upward.exit_ts
    assert base.exit_price == primary.exit_price == upward.exit_price
    assert base.gross_bps == primary.gross_bps == upward.gross_bps
    assert (
        base.all_in_bps == 13.0
        and primary.all_in_bps == 17.0
        and upward.all_in_bps == 22.0
    )
    assert base.net_bps == base.gross_bps - 13.0
    assert primary.net_bps == primary.gross_bps - 17.0
    assert upward.net_bps == upward.gross_bps - 22.0
    assert base.net_bps > primary.net_bps > upward.net_bps


def test_fee_bps_recorded_but_not_subtracted_twice_in_net():
    specs = [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, sl=1000.0, tp=200.0, timeout=1)
    t = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS).trades[0]
    assert t.fee_bps == 10.0
    assert t.net_bps == t.gross_bps - t.all_in_bps - t.funding_bps
    assert t.net_bps != t.gross_bps - t.fee_bps - t.all_in_bps - t.funding_bps


def test_funding_applied_exactly_once_via_lookup():
    specs = [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, sl=1000.0, tp=200.0, timeout=1)
    calls = []

    def lookup(symbol, side, entry_ts, exit_ts):
        calls.append((symbol, side, entry_ts, exit_ts))
        return [
            cm.FundingCrossing(ts=entry_ts + 1, rate_bps=2.0),
            cm.FundingCrossing(ts=entry_ts + 2, rate_bps=1.0),
        ]

    result = run_symbol_stream(
        bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS, funding_lookup=lookup
    )
    t = result.trades[0]
    assert calls == [("XRPUSDT", "long", 0, MIN)]
    assert t.funding_bps == 3.0  # long pays: sum(rate_bps)
    assert t.net_bps == t.gross_bps - t.all_in_bps - 3.0


# --------------------------------------------------------------------------- #
# ROB-942 R1 correction: cost-scenario path divergence is an intended
# consequence of AC8's cost-included daily stop, not a bug -- and the 68bp
# entry-eligibility gate itself does NOT vary by scenario. See the
# rob940_cost_model / rob940_engine module docstrings for the full writeup.
# --------------------------------------------------------------------------- #
def test_68bp_gate_is_identical_across_all_cost_scenarios():
    specs = [(100, 100, 100, 100), (100, 100.1, 99.9, 100)]
    bars = _mk(0, specs)
    for scenario in cm.COST_SCENARIOS:
        sig_pass = _sig(signal_ts=0, tp=68.0, sl=100.0, timeout=1)
        result_pass = run_symbol_stream(bars, [sig_pass], scenario)
        assert len(result_pass.trades) == 1, scenario.name

        sig_fail = _sig(signal_ts=0, tp=67.99, sl=100.0, timeout=1)
        result_fail = run_symbol_stream(bars, [sig_fail], scenario)
        assert result_fail.trades == (), scenario.name
        assert result_fail.no_trades[0].reason == "tp_below_min_distance"


def test_cost_scenario_dependent_daily_stop_diverges_trade_count():
    # trade1: SL touch at sl_distance=1000bps -> gross=-1000bps exactly.
    # trade2: timeout exit at sl_distance=25bps with gross=-3bps (no SL touch).
    # trade3: clean TP hit (gap-through), scenario-independent outcome.
    #
    # cumulative R after trade1+trade2 = -1.12 - 0.041*all_in_bps:
    #   base(13)    -> -1.653  (> -2.0, NOT halted -> trade3 fires)
    #   primary(17) -> -1.817  (> -2.0, NOT halted -> trade3 fires)
    #   upward(22)  -> -2.022  (<= -2.0, HALTED -> trade3 blocked)
    # This is the reproduction from the R1 verify report (base/primary=3
    # trades, upward=2 trades) with hand-derived, exactly-reproducible bars.
    specs = [
        (100, 100, 100, 100),  # idx0: trade1 entry (flat)
        (91, 91.5, 90, 90.5),  # idx1: trade1 SL touch, low<=90
        (100, 100, 100, 100),  # idx2: trade2 entry (flat)
        (99.97, 99.97, 99.97, 99.97),  # idx3: trade2 timeout deadline (open=99.97)
        (100, 100, 100, 100),  # idx4: trade3 entry (flat)
        (102, 102, 102, 102),  # idx5: trade3 deadline, gaps through TP=101
    ]
    bars = _mk(0, specs)
    sig1 = _sig(signal_ts=0, side="long", sl=1000.0, tp=100000.0, timeout=5, cooldown=0)
    sig2 = _sig(
        signal_ts=2 * MIN, side="long", sl=25.0, tp=100000.0, timeout=1, cooldown=0
    )
    sig3 = _sig(
        signal_ts=4 * MIN, side="long", sl=1000.0, tp=100.0, timeout=1, cooldown=0
    )
    signals = [sig1, sig2, sig3]

    results = {s.name: run_symbol_stream(bars, signals, s) for s in cm.COST_SCENARIOS}
    base, primary, upward = (
        results["base"],
        results["primary_stress"],
        results["upward_stress"],
    )

    # trade1/trade2 are eligibility-identical (same 68bp gate, no halt yet
    # reached) across all three scenarios -- only the count diverges via AC8.
    for r in (base, primary, upward):
        assert len(r.trades) >= 2
        assert r.trades[0].exit_reason == "stop_loss"
        assert r.trades[0].exit_ts == base.trades[0].exit_ts == 1 * MIN
        assert r.trades[1].exit_reason == "timeout"
        assert r.trades[1].exit_ts == base.trades[1].exit_ts == 3 * MIN

    assert len(base.trades) == 3
    assert len(primary.trades) == 3
    assert len(upward.trades) == 2
    assert upward.no_trades[-1].reason == "daily_stop_active"
    assert base.no_trades == ()
    assert primary.no_trades == ()

    r1 = base.trades[0].net_bps / 1000.0
    r2 = base.trades[1].net_bps / 25.0
    assert r1 + r2 > -2.0  # base: not halted after trade1+trade2
    r1u = upward.trades[0].net_bps / 1000.0
    r2u = upward.trades[1].net_bps / 25.0
    assert r1u + r2u <= -2.0  # upward: halted after trade1+trade2


# --------------------------------------------------------------------------- #
# ordering / determinism / hash
# --------------------------------------------------------------------------- #
def test_signals_processed_in_signal_ts_order_regardless_of_input_order():
    block = _quick_tp_trade_specs()
    bars = _mk(0, block * 2)
    sig_a = _sig(signal_ts=0, sl=1000.0, tp=200.0, timeout=1)
    sig_b = _sig(signal_ts=2 * MIN, sl=1000.0, tp=200.0, timeout=1)
    forward = run_symbol_stream(bars, [sig_a, sig_b], cm.COST_SCENARIO_PRIMARY_STRESS)
    backward = run_symbol_stream(bars, [sig_b, sig_a], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert forward.trades == backward.trades


def test_ledger_hash_is_deterministic_and_content_sensitive():
    from rob940_engine import ledger_hash

    specs = [(100, 100, 100, 100), (100.5, 102.0, 100.4, 101.9)]
    bars = _mk(0, specs)
    sig = _sig(signal_ts=0, sl=1000.0, tp=200.0, timeout=1)
    r1 = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    r2 = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
    assert ledger_hash(r1.trades) == ledger_hash(r2.trades)

    r3 = run_symbol_stream(bars, [sig], cm.COST_SCENARIO_BASE)  # different scenario
    assert ledger_hash(r1.trades) != ledger_hash(r3.trades)

    assert isinstance(ledger_hash(()), str) and len(ledger_hash(())) == 64


def test_rejects_non_increasing_bar_timestamps():
    bars = _mk(0, [(1, 1, 1, 1), (2, 2, 2, 2)])
    bars = [bars[0], bars[0]]
    sig = _sig(signal_ts=0)
    try:
        run_symbol_stream(bars, [sig], cm.COST_SCENARIO_PRIMARY_STRESS)
        raised = False
    except ValueError:
        raised = True
    assert raised
