"""ROB-979 (H2, ROB-974 R2) CP2 -- S3 account-global portfolio engine (RED first).

Covers ROB-979 AC5-13: one account-global position across XRP/DOGE/SOL,
chronological minute walk, exact boundary precedence (minute-open gap SL/TP ->
completed-4h THESIS_EXIT -> 48h/12-bar TIMEOUT -> remaining-minute intrabar
SL-first/TP), cooldown/day-halt gates, EOF/horizon rejection, one-minute gap
termination, and the closed TP|SL|THESIS_EXIT|TIMEOUT exit-reason set. See
``rob974_h2_s3_engine.py`` module docstring for the ultrathink design log
(MFE/MAE capping, cooldown-boundary rounding, EOF/gap/horizon distinction).
"""

from __future__ import annotations

from rob974_h2_dtos import MinuteBar, S3CloseFeature, S3SignalIntent
from rob974_h2_ingress import build_minute_index
from rob974_h2_s3_engine import FOUR_H_MS, run_s3_portfolio_stream

_MIN_MS = 60_000
_CORPUS_END = 10_000_000_000  # far beyond any test fixture's horizon


def _bars(symbol, start_ts, count, price=1.0, overrides=None):
    overrides = overrides or {}
    out = []
    for i in range(count):
        ts = start_ts + i * _MIN_MS
        o, h, low, c = overrides.get(i, (price, price, price, price))
        out.append(MinuteBar(symbol, ts, o, h, low, c))
    return out


def _intent(symbol="XRPUSDT", side="long", signal_ts=0, sl=0.0080, tp=0.0128, **kw):
    fields = {
        "symbol": symbol,
        "side": side,
        "signal_ts": signal_ts,
        "entry_sl_distance": sl,
        "entry_tp_distance": tp,
        "config_id": "s3-00",
        "fold_id": "fold-00",
        "volatility_percentile": 55.0,
    }
    fields.update(kw)
    return S3SignalIntent(**fields)


def _flat_close_features(symbol, start_ts, n_boundaries, close=1.0, vwap24=1.0, m=0.01):
    """Feature snapshots that never trigger THESIS_EXIT for a long (m>0, close>=vwap24)."""
    out = []
    for k in range(1, n_boundaries + 1):
        out.append(S3CloseFeature(symbol, start_ts + k * FOUR_H_MS, close, vwap24, m))
    return out


def _run(candidates, bars, features, corpus_end_ts=_CORPUS_END, horizon_end_ts=None):
    minute_index = build_minute_index(bars)
    return run_s3_portfolio_stream(
        candidates,
        minute_index,
        {(f.symbol, f.close_ts): f for f in features},
        corpus_end_ts=corpus_end_ts,
        horizon_end_ts=horizon_end_ts,
    )


class TestExactTickEntry:
    def test_missing_exact_tick_is_next_tick_unavailable(self):
        # signal_ts requires a bar AT ts=0; bars start at 60_000 instead (gap at 0).
        bars = _bars("XRPUSDT", _MIN_MS, 5, price=1.0)
        result = _run([_intent(signal_ts=0)], bars, [])
        assert result.trades == ()
        assert len(result.no_trades) == 1
        assert result.no_trades[0].reason == "next_tick_unavailable"

    def test_exact_tick_present_enters(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.05, 1.05, 1.05, 1.05)  # quick gap TP
        result = _run([_intent(signal_ts=0)], bars, [])
        assert len(result.trades) == 1
        assert result.trades[0].entry_price == 1.0
        assert result.trades[0].entry_ts == 0


class TestPrecedenceAndFills:
    def test_gap_through_sl_fills_at_adverse_open(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        # minute 1 (ts=60_000) gaps below SL (sl=1.0*(1-0.008)=0.992) at open=0.90
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 0.90, 0.90, 0.90, 0.90)
        result = _run([_intent(signal_ts=0)], bars, [])
        assert len(result.trades) == 1
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        assert trade.exit_price == 0.90  # adverse-open fill, not the sl barrier
        assert trade.exit_ts == _MIN_MS

    def test_gap_through_tp_fills_at_barrier_not_open(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        tp_price = 1.0 * 1.0128
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.05, 1.05, 1.05, 1.05)  # gaps past TP
        result = _run([_intent(signal_ts=0)], bars, [])
        trade = result.trades[0]
        assert trade.exit_reason == "TP"
        assert abs(trade.exit_price - tp_price) < 1e-9  # barrier, not the 1.05 open

    def test_same_minute_sl_and_tp_resolves_sl_first(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        sl_price = 1.0 * (1 - 0.0080)
        tp_price = 1.0 * (1 + 0.0128)
        # minute 1 spans both barriers intrabar (no gap at open == 1.0)
        bars[1] = MinuteBar(
            "XRPUSDT", _MIN_MS, 1.0, tp_price + 0.01, sl_price - 0.01, 1.0
        )
        result = _run([_intent(signal_ts=0)], bars, [])
        assert result.trades[0].exit_reason == "SL"

    def test_thesis_exit_fills_at_boundary_minute_open_not_4h_close(self):
        bars = _bars("XRPUSDT", 0, 241, price=1.0)
        # the boundary minute (ts=FOUR_H_MS) opens at a DIFFERENT price than 1.0
        # to prove the fill uses the minute open, not the 4h close feature's `close`.
        boundary_open = 1.0005
        bars[240] = MinuteBar(
            "XRPUSDT",
            FOUR_H_MS,
            boundary_open,
            boundary_open,
            boundary_open,
            boundary_open,
        )
        # thesis condition true for long: M_t <= 0 (market regime flipped)
        features = [S3CloseFeature("XRPUSDT", FOUR_H_MS, 1.0, 1.0, -0.001)]
        result = _run([_intent(signal_ts=0)], bars, features)
        trade = result.trades[0]
        assert trade.exit_reason == "THESIS_EXIT"
        assert trade.exit_ts == FOUR_H_MS
        assert trade.exit_price == boundary_open

    def test_thesis_exit_condition_false_falls_through_to_intrabar(self):
        bars = _bars("XRPUSDT", 0, 242, price=1.0)
        sl_price = 1.0 * (1 - 0.0080)
        # boundary minute itself does NOT gap or touch anything
        bars[240] = MinuteBar("XRPUSDT", FOUR_H_MS, 1.0, 1.0, 1.0, 1.0)
        # the NEXT minute (non-boundary) touches SL intrabar
        bars[241] = MinuteBar(
            "XRPUSDT", FOUR_H_MS + _MIN_MS, 1.0, 1.0, sl_price - 0.001, 1.0
        )
        features = _flat_close_features("XRPUSDT", 0, 12)  # thesis stays false (m>0)
        result = _run([_intent(signal_ts=0)], bars, features)
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        assert trade.exit_ts == FOUR_H_MS + _MIN_MS

    def test_precedence_thesis_exit_wins_over_timeout_at_deadline(self):
        deadline_offset = 12 * FOUR_H_MS
        bars = _bars("XRPUSDT", 0, (deadline_offset // _MIN_MS) + 1, price=1.0)
        deadline_open = 1.0002
        bars[deadline_offset // _MIN_MS] = MinuteBar(
            "XRPUSDT",
            deadline_offset,
            deadline_open,
            deadline_open,
            deadline_open,
            deadline_open,
        )
        features = _flat_close_features(
            "XRPUSDT", 0, 11
        )  # boundaries 1..11: no thesis exit
        features.append(
            S3CloseFeature(
                "XRPUSDT", deadline_offset, 1.0, 1.0, -0.01
            )  # boundary 12: thesis true
        )
        result = _run([_intent(signal_ts=0)], bars, features)
        trade = result.trades[0]
        assert trade.exit_reason == "THESIS_EXIT"
        assert trade.exit_ts == deadline_offset

    def test_timeout_at_exact_48h_deadline_when_no_earlier_exit(self):
        deadline_offset = 12 * FOUR_H_MS
        bars = _bars("XRPUSDT", 0, (deadline_offset // _MIN_MS) + 1, price=1.0)
        features = _flat_close_features("XRPUSDT", 0, 12)  # thesis never true
        result = _run([_intent(signal_ts=0)], bars, features)
        trade = result.trades[0]
        assert trade.exit_reason == "TIMEOUT"
        assert trade.exit_ts == deadline_offset
        assert trade.exit_price == 1.0


class TestExitReasonClosedSet:
    def test_only_four_reasons_possible(self):
        bars = _bars("XRPUSDT", 0, 5, price=1.0)
        features = _flat_close_features("XRPUSDT", 0, 12)
        result = _run([_intent(signal_ts=0)], bars, features)
        for t in result.trades:
            assert t.exit_reason in ("TP", "SL", "THESIS_EXIT", "TIMEOUT")


class TestCooldownAndDayGates:
    def test_same_symbol_cooldown_blocks_within_two_boundaries(self):
        # position 1: enters at 0, TP-exits immediately at minute 1 (gap TP)
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.05, 1.05, 1.05, 1.05)
        # candidate 2 for the SAME symbol at signal_ts == 1 boundary later (< 2*4h after exit)
        cand2_ts = FOUR_H_MS
        more_bars = _bars("XRPUSDT", cand2_ts, 3, price=1.0)
        result = _run(
            [_intent(signal_ts=0), _intent(symbol="XRPUSDT", signal_ts=cand2_ts)],
            bars + more_bars,
            [],
        )
        assert len(result.trades) == 1
        reasons = [nt.reason for nt in result.no_trades]
        assert "cooldown_active" in reasons

    def test_same_symbol_cooldown_clears_after_two_boundaries(self):
        bars = _bars("XRPUSDT", 0, 3, price=1.0)
        bars[1] = MinuteBar("XRPUSDT", _MIN_MS, 1.05, 1.05, 1.05, 1.05)
        # exit's containing boundary is 0 (exit at minute 1, still within [0,4h) bucket);
        # cooldown clears at exit_boundary(=4h) + 2*4h = 12h.
        cand2_ts = 3 * FOUR_H_MS
        more_bars = _bars("XRPUSDT", cand2_ts, 3, price=1.0)
        more_bars[1] = MinuteBar(
            "XRPUSDT", cand2_ts + _MIN_MS, 1.05, 1.05, 1.05, 1.05
        )  # quick gap TP so this position resolves within the fixture window
        result = _run(
            [_intent(signal_ts=0), _intent(symbol="XRPUSDT", signal_ts=cand2_ts)],
            bars + more_bars,
            [],
        )
        assert len(result.trades) == 2

    def test_max_two_new_entries_per_entry_utc_date_global(self):
        day0 = 0
        bars_a = _bars("XRPUSDT", day0, 3, price=1.0)
        bars_a[1] = MinuteBar(
            "XRPUSDT", day0 + _MIN_MS, 1.05, 1.05, 1.05, 1.05
        )  # TP exit
        cand2_ts = day0 + FOUR_H_MS
        bars_b = _bars("DOGEUSDT", cand2_ts, 3, price=1.0)
        bars_b[1] = MinuteBar("DOGEUSDT", cand2_ts + _MIN_MS, 1.05, 1.05, 1.05, 1.05)
        cand3_ts = day0 + 2 * FOUR_H_MS
        bars_c = _bars("SOLUSDT", cand3_ts, 3, price=1.0)
        result = _run(
            [
                _intent(symbol="XRPUSDT", signal_ts=day0),
                _intent(symbol="DOGEUSDT", signal_ts=cand2_ts),
                _intent(symbol="SOLUSDT", signal_ts=cand3_ts),
            ],
            bars_a + bars_b + bars_c,
            [],
        )
        assert len(result.trades) == 2
        reasons = [nt.reason for nt in result.no_trades]
        assert "daily_entry_cap" in reasons

    def test_second_sl_same_exit_date_halts_further_entries_that_date(self):
        day0 = 0
        sl_price = 1.0 * (1 - 0.0080)
        bars_a = _bars("XRPUSDT", day0, 3, price=1.0)
        bars_a[1] = MinuteBar(
            "XRPUSDT", day0 + _MIN_MS, sl_price, sl_price, sl_price, sl_price
        )
        cand2_ts = day0 + FOUR_H_MS
        bars_b = _bars("DOGEUSDT", cand2_ts, 3, price=1.0)
        bars_b[1] = MinuteBar(
            "DOGEUSDT", cand2_ts + _MIN_MS, sl_price, sl_price, sl_price, sl_price
        )
        cand3_ts = day0 + 2 * FOUR_H_MS
        bars_c = _bars("SOLUSDT", cand3_ts, 3, price=1.0)
        result = _run(
            [
                _intent(symbol="XRPUSDT", signal_ts=day0),
                _intent(symbol="DOGEUSDT", signal_ts=cand2_ts),
                _intent(symbol="SOLUSDT", signal_ts=cand3_ts),
            ],
            bars_a + bars_b + bars_c,
            [],
        )
        assert len(result.trades) == 2
        assert all(t.exit_reason == "SL" for t in result.trades)
        reasons = [nt.reason for nt in result.no_trades]
        assert "sl_halt_active" in reasons


class TestSameTickArbitration:
    def test_exit_finalizes_before_same_tick_new_candidate_from_flat_state(self):
        # position on XRP TP-exits exactly at ts=FOUR_H_MS via boundary THESIS_EXIT-less
        # gap-TP; a DOGE candidate signals at that exact same instant and must be
        # allowed to enter (global-flat state), not rejected as "position open".
        bars_xrp = _bars("XRPUSDT", 0, 241, price=1.0)
        bars_xrp[240] = MinuteBar(
            "XRPUSDT", FOUR_H_MS, 1.05, 1.05, 1.05, 1.05
        )  # gap TP
        bars_doge = _bars("DOGEUSDT", FOUR_H_MS, 3, price=0.5)
        bars_doge[1] = MinuteBar(
            "DOGEUSDT", FOUR_H_MS + _MIN_MS, 0.55, 0.55, 0.55, 0.55
        )  # quick gap TP so this position also resolves within the fixture window
        result = _run(
            [
                _intent(symbol="XRPUSDT", signal_ts=0),
                _intent(symbol="DOGEUSDT", signal_ts=FOUR_H_MS),
            ],
            bars_xrp + bars_doge,
            _flat_close_features("XRPUSDT", 0, 12),
        )
        assert len(result.trades) == 2

    def test_candidate_before_open_position_exit_is_rejected(self):
        bars_xrp = _bars("XRPUSDT", 0, 3, price=1.0)
        bars_xrp[2] = MinuteBar(
            "XRPUSDT", 2 * _MIN_MS, 1.05, 1.05, 1.05, 1.05
        )  # gap TP
        bars_doge = _bars("DOGEUSDT", _MIN_MS, 5, price=0.5)  # signals mid-hold
        result = _run(
            [
                _intent(symbol="XRPUSDT", signal_ts=0),
                _intent(symbol="DOGEUSDT", signal_ts=_MIN_MS),
            ],
            bars_xrp + bars_doge,
            [],
        )
        assert len(result.trades) == 1
        reasons = [nt.reason for nt in result.no_trades]
        assert "global_position_open" in reasons


class TestGapAndHorizon:
    def test_one_minute_gap_in_open_position_is_terminal(self):
        bars = _bars(
            "XRPUSDT", 0, 2, price=1.0
        )  # only ts=0,60000 present; gap at 120000
        result = _run([_intent(signal_ts=0)], bars, [])
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "data_gap_in_position"

    def test_early_eof_at_corpus_end_is_not_data_gap(self):
        bars = _bars("XRPUSDT", 0, 2, price=1.0)
        result = _run([_intent(signal_ts=0)], bars, [], corpus_end_ts=120_000)
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "early_eof"

    def test_fold_horizon_rejected_before_data_gap(self):
        # bars cover [0, 60_000]; a gap exists at 120_000 -- but the horizon
        # (EXCLUSIVE at 60_000) forbids reading 60_000 onwards at all, so the
        # rejection must be fold_horizon_rejected, never a
        # data_gap_in_position for the (never-reached) 120_000 gap.
        bars = _bars("XRPUSDT", 0, 2, price=1.0)
        result = _run([_intent(signal_ts=0)], bars, [], horizon_end_ts=60_000)
        assert result.incompletes[0].reason == "fold_horizon_rejected"

    def test_missing_future_feature_at_boundary_is_missing_future_data(self):
        bars = _bars("XRPUSDT", 0, 241, price=1.0)
        # no S3CloseFeature supplied for the boundary close_ts -> feature missing
        result = _run([_intent(signal_ts=0)], bars, [])
        assert result.incompletes[0].reason == "missing_future_data"


class TestHorizonExclusiveEnd:
    """ROB-974 R3 boundary fix -- ``horizon_end_ts`` is an EXCLUSIVE end.

    This class previously asserted the opposite (``horizon_end_ts ==
    deadline`` still resolves a TIMEOUT), on the R2 reading that D1 had
    approved an INCLUSIVE fold boundary.  That reading is incompatible with
    the phase contract the engine actually runs under: callers pass
    ``phase.end_ms``, and ``rob974_h4_runner.build_actual_h1_phase_context``
    RAISES on any ``row.ts >= phase.end_ms``, so the ``phase_end`` bar is
    never in the index and reading it is a genuine horizon violation.  A
    deadline bar is only readable when the horizon ends strictly after it.
    """

    def test_horizon_one_minute_past_deadline_resolves_timeout(self):
        deadline_offset = 12 * FOUR_H_MS
        bars = _bars("XRPUSDT", 0, (deadline_offset // _MIN_MS) + 1, price=1.0)
        features = _flat_close_features("XRPUSDT", 0, 12)
        result = _run(
            [_intent(signal_ts=0)],
            bars,
            features,
            horizon_end_ts=deadline_offset + _MIN_MS,
        )
        assert len(result.trades) == 1
        assert result.trades[0].exit_reason == "TIMEOUT"
        assert result.trades[0].exit_ts == deadline_offset
        assert result.incompletes == ()

    def test_horizon_exact_equal_to_deadline_is_rejected(self):
        deadline_offset = 12 * FOUR_H_MS
        bars = _bars("XRPUSDT", 0, (deadline_offset // _MIN_MS) + 1, price=1.0)
        features = _flat_close_features("XRPUSDT", 0, 12)
        result = _run(
            [_intent(signal_ts=0)], bars, features, horizon_end_ts=deadline_offset
        )
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "fold_horizon_rejected"

    def test_horizon_one_ms_past_deadline_is_rejected(self):
        deadline_offset = 12 * FOUR_H_MS
        bars = _bars("XRPUSDT", 0, (deadline_offset // _MIN_MS) + 1, price=1.0)
        features = _flat_close_features("XRPUSDT", 0, 12)
        result = _run(
            [_intent(signal_ts=0)],
            bars,
            features,
            horizon_end_ts=deadline_offset - 1,
        )
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "fold_horizon_rejected"


class TestIdentityCollision:
    def test_duplicate_candidate_identity_raises(self):
        bars = _bars("XRPUSDT", 0, 5, price=1.0)
        import pytest

        with pytest.raises(ValueError):
            _run([_intent(signal_ts=0), _intent(signal_ts=0)], bars, [])


class TestMfeMaeCapping:
    def test_mfe_mae_do_not_leak_past_exit(self):
        bars = _bars("XRPUSDT", 0, 4, price=1.0)
        sl_price = 1.0 * (1 - 0.0080)
        bars[1] = MinuteBar(
            "XRPUSDT", _MIN_MS, 1.0, 1.0, sl_price, 1.0
        )  # intrabar SL touch
        # a LATER bar (chronologically after exit) has an extreme favorable high --
        # must never be walked/considered since the engine stops at exit.
        bars[2] = MinuteBar("XRPUSDT", 2 * _MIN_MS, 5.0, 5.0, 5.0, 5.0)
        result = _run([_intent(signal_ts=0)], bars, [])
        trade = result.trades[0]
        assert trade.exit_reason == "SL"
        # mfe must reflect only entry(1.0)->sl_price path, never the 5.0 bar.
        assert trade.mfe_bps < 50.0


class TestProductionPhaseBoundaryShape:
    """ROB-974 R3 boundary fix -- the PRODUCTION shape, not a synthetic one.

    ``TestGapAndHorizon``/``TestHorizonExclusiveEnd`` above each exercise one
    half in isolation: an artificially punched minute_index for the gap
    reason, and a horizon set to an arbitrary offset for the horizon reason.
    Neither reproduces the fact that in production the two are the SAME
    VALUE -- ``rob974_h6b_materializer._actual_execution_surface`` builds the
    index from ``row.ts < phase.end_ms`` and then passes
    ``horizon_end_ts=phase.end_ms`` to this engine.  A position that survives
    to the phase boundary therefore meets the horizon guard and the index
    cut-off at the identical timestamp, and whichever guard is evaluated
    first decides the reason code.

    Under the pre-fix ``next_ts > horizon_end_ts`` these tests produced
    ``data_gap_in_position`` -- a corpus-defect claim over a corpus with zero
    missing minutes -- and ``fold_horizon_rejected`` was unreachable.
    """

    _PHASE_END = 5 * _MIN_MS

    def _phase_bars(self):
        """Exactly the materializer's cut: every minute with ts < phase_end."""
        return _bars("XRPUSDT", 0, self._PHASE_END // _MIN_MS, price=1.0)

    def test_position_surviving_to_phase_end_is_horizon_rejected_not_data_gap(self):
        bars = self._phase_bars()
        assert max(b.open_time for b in bars) == self._PHASE_END - _MIN_MS
        result = _run([_intent(signal_ts=0)], bars, [], horizon_end_ts=self._PHASE_END)
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "fold_horizon_rejected"

    def test_real_mid_index_hole_is_still_a_data_gap(self):
        """Opposite-direction guard: the fix must not relabel true gaps."""
        bars = [
            b for b in _bars("XRPUSDT", 0, 8, price=1.0) if b.open_time != 2 * _MIN_MS
        ]
        result = _run([_intent(signal_ts=0)], bars, [], horizon_end_ts=100 * _MIN_MS)
        assert result.trades == ()
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "data_gap_in_position"

    def test_horizon_equal_to_corpus_end_reports_horizon_not_eof(self):
        """``rob974_h4_pbo`` full-window shape: horizon == corpus_end.

        Both guards now use ``>=``, so they collide when a caller passes the
        same value for both (``WINDOW_END_MS``).  The horizon guard is
        evaluated first and wins.  Pinned so the precedence is a reviewed
        decision rather than an accident of statement order.
        """
        bars = self._phase_bars()
        result = _run(
            [_intent(signal_ts=0)],
            bars,
            [],
            corpus_end_ts=self._PHASE_END,
            horizon_end_ts=self._PHASE_END,
        )
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "fold_horizon_rejected"

    def test_horizon_absent_still_reports_early_eof_at_corpus_end(self):
        """No horizon supplied -> corpus exhaustion keeps its own reason."""
        bars = self._phase_bars()
        result = _run([_intent(signal_ts=0)], bars, [], corpus_end_ts=self._PHASE_END)
        assert len(result.incompletes) == 1
        assert result.incompletes[0].reason == "early_eof"
