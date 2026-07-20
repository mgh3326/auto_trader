"""ROB-979 (H2, ROB-974 R2) CP3 -- S4 historical pair-basket engine (RED first).

Covers ROB-979 AC14-24: one global pair basket at a time, synchronized
same-minute atomic entry (either leg missing the exact tick -> NO_TRADE),
entry-frozen weights/G defensive re-validation, conservative-bound G_min/
G_max basket-return evaluation (SL any-possible-touch vs TP only-if-
guaranteed), exact boundary precedence (gap -> MEAN_EXIT -> STALL_EXIT ->
36h/9-bar TIMEOUT -> remaining-minute bounds), one-leg gap termination, and
the frozen historical-null execution posture. See ``rob974_h2_s4_engine.py``
module docstring for the ultrathink design log (G_min/G_max bound
derivation, why S4 has NO cooldown/day-cap gates unlike S3).
"""

from __future__ import annotations

import math

import pytest
from rob974_h2_dtos import MinuteBar, S4PairLegClose, S4PairSignalIntent
from rob974_h2_ingress import build_minute_index
from rob974_h2_s4_engine import FOUR_H_MS, run_s4_pair_basket_stream

_MIN_MS = 60_000
_CORPUS_END = 10_000_000_000
_PAIR = ("XRPUSDT", "DOGEUSDT")


def _bars(symbol, start_ts, count, price=1.0, overrides=None):
    overrides = overrides or {}
    out = []
    for i in range(count):
        ts = start_ts + i * _MIN_MS
        o, h, low, c = overrides.get(i, (price, price, price, price))
        out.append(MinuteBar(symbol, ts, o, h, low, c))
    return out


def _intent(
    pair=_PAIR,
    signal_ts=0,
    side_a="short",
    side_b="long",
    weight_a=0.4,
    weight_b=0.6,
    mu=0.0,
    sigma=0.05,
    z_entry=1.9,
    sl=0.0100,
    tp=0.0150,
    **kw,
):
    fields = {
        "pair": pair,
        "signal_ts": signal_ts,
        "side_a": side_a,
        "side_b": side_b,
        "weight_a": weight_a,
        "weight_b": weight_b,
        "beta_a": 1.2,
        "beta_b": 0.8,
        "mu": mu,
        "sigma": sigma,
        "z_entry": z_entry,
        "gross_notional": max(6 / weight_a, 6 / weight_b),
        "entry_sl_distance": sl,
        "entry_tp_distance": tp,
        "config_id": "s4-00",
        "fold_id": "fold-00",
    }
    fields.update(kw)
    return S4PairSignalIntent(**fields)


def _run(candidates, bars, pair_closes, corpus_end_ts=_CORPUS_END, horizon_end_ts=None):
    minute_index = build_minute_index(bars)
    pair_close_index = {(c.symbol, c.close_ts): c for c in pair_closes}
    return run_s4_pair_basket_stream(
        candidates,
        minute_index,
        pair_close_index,
        corpus_end_ts=corpus_end_ts,
        horizon_end_ts=horizon_end_ts,
    )


def _flat_pair_closes(pair, start_ts, n_boundaries, close_a=1.0, close_b=1.0):
    out = []
    for k in range(1, n_boundaries + 1):
        ts = start_ts + k * FOUR_H_MS
        out.append(S4PairLegClose(pair[0], ts, close_a))
        out.append(S4PairLegClose(pair[1], ts, close_b))
    return out


class TestSynchronizedEntry:
    def test_both_legs_present_enters(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.20, 1.20, 1.20, 1.20)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 1.20, 1.20, 1.20, 1.20)
        # short a / long b, both legs move +20% -> G = -0.4*ln(1.2)+0.6*ln(1.2) = 0.2*ln(1.2)
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.entry_price_a == 1.0
        assert trade.entry_price_b == 1.0

    def test_leg_a_missing_exact_tick_is_no_trade(self):
        bars_a = _bars("XRPUSDT", _MIN_MS, 3, price=1.0)  # gap at ts=0
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        assert result.trades == ()
        assert result.no_trades[0].reason == "next_tick_unavailable"

    def test_leg_b_missing_exact_tick_is_no_trade(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_b = _bars("DOGEUSDT", _MIN_MS, 3, price=1.0)  # gap at ts=0
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        assert result.trades == ()
        assert result.no_trades[0].reason == "next_tick_unavailable"


class TestGFeasibilityDefensiveGuard:
    def test_infeasible_g_bounds_rejected(self):
        # weight_a tiny -> G_min=max(6/wa,6/wb) exceeds G_max=min(10/wa,10/wb)
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        cand = _intent(signal_ts=0, weight_a=0.05, weight_b=0.95, gross_notional=120.0)
        result = _run([cand], bars_a + bars_b, [])
        assert result.trades == ()
        assert result.no_trades[0].reason == "g_infeasible"

    def test_mismatched_gross_notional_rejected(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        cand = _intent(signal_ts=0, weight_a=0.4, weight_b=0.6, gross_notional=999.0)
        result = _run([cand], bars_a + bars_b, [])
        assert result.trades == ()
        assert result.no_trades[0].reason == "g_mismatch"


class TestGapFills:
    def test_gap_sl_fills_at_real_uncapped_open_value(self):
        # short a / long b; adverse move = a UP a lot, b DOWN a lot
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.5, 1.5, 1.5, 1.5)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 0.6, 0.6, 0.6, 0.6)
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        g_open = -0.4 * math.log(1.5 / 1.0) + 0.6 * math.log(0.6 / 1.0)
        assert abs(trade.gross_bps - g_open * 1e4) < 1e-6
        assert (
            g_open * 1e4 < -100.0
        )  # confirm it really is worse than the -100bp SL barrier

    def test_gap_tp_fills_capped_at_barrier(self):
        # short a / long b; favorable move = a DOWN, b UP hugely (real G far exceeds TP)
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.5, 0.5, 0.5, 0.5)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 2.0, 2.0, 2.0, 2.0)
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        trade = result.trades[0]
        assert trade.exit_reason == "TP"
        assert (
            abs(trade.gross_bps - 150.0) < 1e-6
        )  # capped at d_TP=150bp, not the real windfall


class TestConservativeBounds:
    def test_sl_recognized_on_any_possible_adverse_touch(self):
        # intrabar (no gap at open==1.0 for either leg); leg A's high and leg B's
        # low make the WORST-CASE bound breach -100bp SL even though the bar's
        # open/close never do.
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar(
            "XRPUSDT", _MIN_MS, 1.0, 1.6, 1.0, 1.0
        )  # k_a=-0.4 (short) -> H is adverse
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar(
            "DOGEUSDT", _MIN_MS, 1.0, 1.0, 0.6, 1.0
        )  # k_b=+0.6 (long) -> L is adverse
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        assert (
            abs(trade.gross_bps - (-100.0)) < 1e-6
        )  # fills at the barrier, not the raw bound

    def test_tp_requires_worst_case_bound_not_just_best_case(self):
        # best-case (G_max) bound clears TP, but worst-case (G_min) does not ->
        # must NOT recognize TP this minute; falls through to the next minute.
        bars_a = _bars("XRPUSDT", 0, 4, price=1.0)
        # minute1: leg A low is very favorable (short a wants low P_a), but leg A
        # high stays near open -> G_min bound (uses HIGH for k_a<0) stays modest.
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.0, 1.01, 0.3, 1.0)
        bars_b = _bars("DOGEUSDT", 0, 4, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 1.0, 1.01, 1.0, 1.0)
        # minute2: nothing eventful -> still no exit; test just needs "not resolved at minute1"
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        # depending on bar2, the position may still be open (incomplete) or exit later --
        # the key assertion is it did NOT exit as TP at minute1.
        if result.trades:
            assert (
                result.trades[0].exit_ts != _MIN_MS
                or result.trades[0].exit_reason != "TP"
            )


class TestBoundaryExits:
    def test_mean_exit_when_z_frozen_reverts_within_quarter_sigma(self):
        bars_a = _bars("XRPUSDT", 0, 241, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 241, price=1.0)
        # entry mu=0, sigma=0.05, so z_frozen<=0.25 needs |s_ab_tau| <= 0.0125
        # s_ab = 0.4*ln(Ca) - 0.6*ln(Cb); choose Ca=Cb=1.0 at boundary -> s_ab=0 -> z=0
        pair_closes = _flat_pair_closes(_PAIR, 0, 1, close_a=1.0, close_b=1.0)
        result = _run(
            [_intent(signal_ts=0, mu=0.0, sigma=0.05)], bars_a + bars_b, pair_closes
        )
        trade = result.trades[0]
        assert trade.exit_reason == "MEAN_EXIT"
        assert trade.exit_ts == FOUR_H_MS

    def test_stall_exit_only_after_two_boundaries(self):
        bars_a = _bars("XRPUSDT", 0, 481, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 481, price=1.0)
        # z stays exactly at entry level (no convergence, no mean revert) at every boundary
        far_ca, far_cb = (
            math.exp(2.0),
            1.0,
        )  # s_ab = 0.4*2.0 - 0 = 0.8; with mu=0,sigma=0.05 -> z=16 (>>z_entry)
        pair_closes = _flat_pair_closes(_PAIR, 0, 2, close_a=far_ca, close_b=far_cb)
        cand = _intent(signal_ts=0, mu=0.0, sigma=0.05, z_entry=1.9)
        result = _run([cand], bars_a + bars_b, pair_closes)
        trade = result.trades[0]
        # boundary 1: |z_frozen|=16 > 0.85*1.9=1.615 but STALL only eligible from boundary>=2
        # boundary 2: same non-convergence -> STALL_EXIT fires exactly there.
        assert trade.exit_reason == "STALL_EXIT"
        assert trade.exit_ts == 2 * FOUR_H_MS

    def test_timeout_at_ninth_boundary_36h(self):
        deadline_offset = 9 * FOUR_H_MS
        n = deadline_offset // _MIN_MS + 1
        bars_a = _bars("XRPUSDT", 0, n, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, n, price=1.0)
        # z never converges (MEAN) and never triggers STALL: hold |z_frozen| in the
        # narrow band (0.25, 0.85*z_entry] at every boundary via close_a slightly off mu.
        pair_closes = []
        for k in range(1, 10):
            ts = k * FOUR_H_MS
            pair_closes.append(S4PairLegClose("XRPUSDT", ts, math.exp(0.05)))
            pair_closes.append(S4PairLegClose("DOGEUSDT", ts, 1.0))
        cand = _intent(signal_ts=0, mu=0.0, sigma=0.05, z_entry=1.9)
        result = _run([cand], bars_a + bars_b, pair_closes)
        trade = result.trades[0]
        assert trade.exit_reason == "TIMEOUT"
        assert trade.exit_ts == deadline_offset


class TestHorizonExactEquality:
    """verify-R1 finding 1: D1 approves signal_ts+strategy_max_hold==phase_end
    as READABLE; only strictly overrunning phase_end is a horizon violation."""

    @staticmethod
    def _no_convergence_fixture():
        deadline_offset = 9 * FOUR_H_MS
        n = deadline_offset // _MIN_MS + 1
        bars_a = _bars("XRPUSDT", 0, n, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, n, price=1.0)
        pair_closes = []
        for k in range(1, 10):
            ts = k * FOUR_H_MS
            pair_closes.append(S4PairLegClose("XRPUSDT", ts, math.exp(0.05)))
            pair_closes.append(S4PairLegClose("DOGEUSDT", ts, 1.0))
        cand = _intent(signal_ts=0, mu=0.0, sigma=0.05, z_entry=1.9)
        return deadline_offset, bars_a + bars_b, pair_closes, cand

    def test_horizon_exact_equal_to_deadline_still_resolves_timeout(self):
        deadline_offset, bars, pair_closes, cand = self._no_convergence_fixture()
        result = _run([cand], bars, pair_closes, horizon_end_ts=deadline_offset)
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "TIMEOUT"
        assert result.trades[0].exit_ts == deadline_offset
        assert result.incompletes == ()

    def test_horizon_one_ms_past_deadline_is_rejected(self):
        deadline_offset, bars, pair_closes, cand = self._no_convergence_fixture()
        result = _run([cand], bars, pair_closes, horizon_end_ts=deadline_offset - 1)
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "fold_horizon_rejected"


class TestExitReasonClosedSet:
    def test_only_five_reasons_possible(self):
        # verify-R1 finding 3 (false-green): `for t in ()` never executed.
        # Replaced with five REAL, independent, fresh engine runs -- one per
        # exit reason -- so this actually exercises the full closed set.
        trades = []

        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.5, 0.5, 0.5, 0.5)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 2.0, 2.0, 2.0, 2.0)
        trades += _run([_intent(signal_ts=0)], bars_a + bars_b, []).trades  # TP

        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.5, 1.5, 1.5, 1.5)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 0.6, 0.6, 0.6, 0.6)
        trades += _run([_intent(signal_ts=0)], bars_a + bars_b, []).trades  # SL

        bars_a = _bars("XRPUSDT", 0, 241, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 241, price=1.0)
        pair_closes = _flat_pair_closes(_PAIR, 0, 1, close_a=1.0, close_b=1.0)
        trades += _run(
            [_intent(signal_ts=0, mu=0.0, sigma=0.05)], bars_a + bars_b, pair_closes
        ).trades  # MEAN_EXIT

        bars_a = _bars("XRPUSDT", 0, 481, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 481, price=1.0)
        far_ca, far_cb = math.exp(2.0), 1.0
        pair_closes = _flat_pair_closes(_PAIR, 0, 2, close_a=far_ca, close_b=far_cb)
        cand = _intent(signal_ts=0, mu=0.0, sigma=0.05, z_entry=1.9)
        trades += _run([cand], bars_a + bars_b, pair_closes).trades  # STALL_EXIT

        deadline_offset = 9 * FOUR_H_MS
        n = deadline_offset // _MIN_MS + 1
        bars_a = _bars("XRPUSDT", 0, n, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, n, price=1.0)
        pair_closes = []
        for k in range(1, 10):
            ts = k * FOUR_H_MS
            pair_closes.append(S4PairLegClose("XRPUSDT", ts, math.exp(0.05)))
            pair_closes.append(S4PairLegClose("DOGEUSDT", ts, 1.0))
        cand = _intent(signal_ts=0, mu=0.0, sigma=0.05, z_entry=1.9)
        trades += _run([cand], bars_a + bars_b, pair_closes).trades  # TIMEOUT

        assert len(trades) == 5
        assert {t.exit_reason for t in trades} == {
            "TP",
            "SL",
            "MEAN_EXIT",
            "STALL_EXIT",
            "TIMEOUT",
        }
        for t in trades:
            assert t.exit_reason in ("TP", "SL", "MEAN_EXIT", "STALL_EXIT", "TIMEOUT")


class TestOneLegGap:
    def test_one_leg_gap_in_open_position_terminal(self):
        bars_a = _bars("XRPUSDT", 0, 2, price=1.0)  # gap at minute 2 for leg a
        bars_b = _bars("DOGEUSDT", 0, 5, price=1.0)  # leg b has plenty of data
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "data_gap_in_pair_position"


class TestSameTickArbitrationAndIdentity:
    def test_duplicate_candidate_identity_raises(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        with pytest.raises(ValueError):
            _run([_intent(signal_ts=0), _intent(signal_ts=0)], bars_a + bars_b, [])

    def test_only_one_global_pair_basket_at_a_time(self):
        # position 1 resolves quickly via a gap TP at minute 1, so its
        # exit_ts (= 60_000) is known -- candidate 2 signals strictly BEFORE
        # that exit_ts and must be rejected as still-open.
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.5, 0.5, 0.5, 0.5)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 2.0, 2.0, 2.0, 2.0)
        cand2 = _intent(pair=("XRPUSDT", "SOLUSDT"), signal_ts=0)
        bars_c = _bars("SOLUSDT", 0, 3, price=1.0)
        result = _run([_intent(signal_ts=0), cand2], bars_a + bars_b + bars_c, [])
        assert len(result.trades) == 1
        reasons = [nt.reason for nt in result.no_trades]
        assert "global_position_open" in reasons


class TestHistoricalNullPosture:
    def test_trade_carries_frozen_historical_null_fields(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.20, 1.20, 1.20, 1.20)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 0.80, 0.80, 0.80, 0.80)
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        trade = result.trades[0]
        assert trade.order_id_a is None
        assert trade.order_id_b is None
        assert trade.pair_exec_status == "historical_atomic_assumption"
        assert trade.pair_executor_validated is False
        assert trade.demo_eligible is False
        assert trade.volatility_percentile is None
        assert trade.volatility_percentile_provenance == "not_defined_for_s4"

    def test_trade_carries_entry_frozen_research_provenance_and_pair_exec_fail(self):
        bars_a = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.20, 1.20, 1.20, 1.20)
        bars_b = _bars("DOGEUSDT", 0, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 0.80, 0.80, 0.80, 0.80)
        cand = _intent(
            signal_ts=0, beta_a=1.3, beta_b=0.7, mu=0.02, sigma=0.06, z_entry=2.1
        )
        result = _run([cand], bars_a + bars_b, [])
        trade = result.trades[0]
        assert trade.beta_a == 1.3
        assert trade.beta_b == 0.7
        assert trade.mu == 0.02
        assert trade.sigma == 0.06
        assert trade.z_entry == 2.1
        assert trade.gross_notional == cand.gross_notional
        assert trade.pair_exec_fail == "not_evaluated"
        assert trade.promotion_status == "promotion_blocked_pending_pair_executor"


class TestMfeMaeCapping:
    def test_mfe_mae_do_not_leak_past_exit(self):
        bars_a = _bars("XRPUSDT", 0, 4, price=1.0)
        bars_a[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.5, 1.5, 1.5, 1.5)  # gap SL leg
        bars_a[2] = MinuteBar(
            "XRPUSDT", 2 * _MIN_MS, 0.01, 0.01, 0.01, 0.01
        )  # post-exit extreme
        bars_b = _bars("DOGEUSDT", 0, 4, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", _MIN_MS, 0.6, 0.6, 0.6, 0.6)
        result = _run([_intent(signal_ts=0)], bars_a + bars_b, [])
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        assert trade.mfe_bps < 50.0
