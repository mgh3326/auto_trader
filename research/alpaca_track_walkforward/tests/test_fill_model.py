"""ROB-1062 H4 (AC11-AC16) — historical fill model boundary tests."""

from __future__ import annotations

import fill_model as fm
import pytest
from daily_bars import SpotMinute

_DECISION_TS = 1_700_000_100_000  # arbitrary minute-aligned ms, unrelated to wall clock
_MIN = fm.MINUTE_MS


def _minute(offset_minutes: int, *, open_: float) -> SpotMinute:
    ts = _DECISION_TS + offset_minutes * _MIN
    low = open_ / 2.0
    return SpotMinute(
        open_time_ms=ts, open=open_, high=open_ + 1.0, low=low, close=open_, volume=1.0
    )


def test_entry_fills_at_first_bar_open_when_at_the_exact_limit_cap_boundary():
    ref = 100.0
    limit_cap = ref * 1.005  # == 100.5
    bars = [_minute(1, open_=limit_cap), _minute(2, open_=999.0)]
    outcome = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is True
    assert outcome.fill_price == limit_cap
    assert outcome.fill_bar_offset == 1
    assert outcome.reason == "FILLED"


def test_entry_rejects_first_bar_one_cent_above_cap_but_fills_second_bar():
    ref = 100.0
    limit_cap = ref * 1.005
    bars = [_minute(1, open_=limit_cap + 0.01), _minute(2, open_=limit_cap)]
    outcome = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is True
    assert outcome.fill_bar_offset == 2
    assert outcome.fill_price == limit_cap


def test_entry_unfilled_when_both_window_bars_exceed_cap():
    ref = 100.0
    bars = [_minute(1, open_=200.0), _minute(2, open_=200.0)]
    outcome = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is False
    assert outcome.reason == "ENTRY_UNFILLED"
    assert outcome.fill_price is None


def test_entry_never_considers_a_third_bar_even_if_it_would_fill():
    """AC16 2-bar window boundary — a widened window is a required mutation
    to kill; this test is the kill mechanism."""
    ref = 100.0
    bars = [
        _minute(1, open_=200.0),
        _minute(2, open_=200.0),
        _minute(3, open_=100.0),  # would fill if the window were widened to 3
    ]
    outcome = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is False
    assert outcome.reason == "ENTRY_UNFILLED"


def test_entry_incomplete_when_a_window_minute_bar_is_missing_not_unfilled():
    """AC15 — a data gap must never be reported as ENTRY_UNFILLED (a real
    market outcome); it is a distinct structural classification."""
    ref = 100.0
    bars = [_minute(1, open_=200.0)]  # bar at offset 2 is missing (gap)
    outcome = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is False
    assert outcome.reason == "FILL_WINDOW_INCOMPLETE"


def test_entry_incomplete_when_both_window_bars_are_missing():
    outcome = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS, reference_close=100.0, minute_bars_after_signal=()
    )
    assert outcome.reason == "FILL_WINDOW_INCOMPLETE"


def test_exit_fills_at_first_bar_open_when_at_the_exact_limit_floor_boundary():
    ref = 100.0
    limit_floor = ref * 0.995  # == 99.5
    bars = [_minute(1, open_=limit_floor), _minute(2, open_=1.0)]
    outcome = fm.model_exit_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is True
    assert outcome.fill_price == limit_floor
    assert outcome.fill_bar_offset == 1


def test_exit_unfilled_when_both_window_bars_below_floor():
    ref = 100.0
    bars = [_minute(1, open_=1.0), _minute(2, open_=1.0)]
    outcome = fm.model_exit_fill(
        decision_ts_ms=_DECISION_TS, reference_close=ref, minute_bars_after_signal=bars
    )
    assert outcome.filled is False
    assert outcome.reason == "EXIT_UNFILLED"


def test_signal_timestamp_bar_open_is_rejected_fail_closed():
    """Verifier reproduction: the same-minute open must never fill."""
    ref = 100.0
    bars = [
        _minute(0, open_=ref),  # forbidden same-timestamp open
        _minute(1, open_=200.0),
        _minute(2, open_=ref),
    ]
    with pytest.raises(ValueError, match="strictly after"):
        fm.model_entry_fill(
            decision_ts_ms=_DECISION_TS,
            reference_close=ref,
            minute_bars_after_signal=bars,
        )


def test_partial_fill_is_never_modeled_literal_flag_on_every_outcome():
    filled = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS,
        reference_close=100.0,
        minute_bars_after_signal=[_minute(1, open_=100.0), _minute(2, open_=100.0)],
    )
    unfilled = fm.model_entry_fill(
        decision_ts_ms=_DECISION_TS,
        reference_close=100.0,
        minute_bars_after_signal=[_minute(1, open_=999.0), _minute(2, open_=999.0)],
    )
    assert filled.partial_fill_modeled is False
    assert unfilled.partial_fill_modeled is False


def test_fill_outcome_construction_rejects_inconsistent_filled_and_price():
    with pytest.raises(ValueError, match="fill_price and fill_bar_offset"):
        fm.FillOutcome(
            filled=True, fill_price=None, fill_bar_offset=None, reason="FILLED"
        )
    with pytest.raises(ValueError, match="fill_price=None and fill_bar_offset=None"):
        fm.FillOutcome(
            filled=False, fill_price=1.0, fill_bar_offset=1, reason="ENTRY_UNFILLED"
        )


def test_fill_outcome_construction_rejects_reason_action_mismatch():
    with pytest.raises(ValueError, match="reason='FILLED' requires filled=True"):
        fm.FillOutcome(
            filled=False, fill_price=None, fill_bar_offset=None, reason="FILLED"
        )
    with pytest.raises(ValueError, match="filled outcome must carry reason='FILLED'"):
        fm.FillOutcome(
            filled=True, fill_price=1.0, fill_bar_offset=1, reason="ENTRY_UNFILLED"
        )
