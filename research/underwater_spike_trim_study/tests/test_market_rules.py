"""KR limit-lock and calendar-contiguity handling — the two honesty rules.

Operator instruction (§129차 ②, KR/US extension):
  ① a KR limit-up locked day is price-unreachable, so no fill may be assumed;
  ② a gap (open != previous close) must also be reported on an open basis.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from research.underwater_spike_trim_study.corpora import (
    SymbolBars,
    _kr_limit_flags,
    _mark_contiguity,
)
from research.underwater_spike_trim_study.events import scan_symbol
from research.underwater_spike_trim_study.spec import BASIS_EVENT_CLOSE, BASIS_NEXT_OPEN


def _kr_frame(
    closes: list[float], zero_range_at: set[int] | None = None
) -> pd.DataFrame:
    zero_range_at = zero_range_at or set()
    rows = []
    for i, close in enumerate(closes):
        if i in zero_range_at:
            rows.append((close, close, close, close))
        else:
            rows.append((close * 0.99, close * 1.02, close * 0.97, close))
    frame = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    frame.insert(0, "session", pd.bdate_range("2020-01-02", periods=len(closes)))
    frame["volume"] = 1000.0
    return frame


def test_limit_up_lock_needs_both_zero_range_and_the_statutory_cap():
    frame = _kr_frame([100, 130, 130 * 1.3, 200], zero_range_at={2})
    flags = _kr_limit_flags(frame)
    assert flags.tolist() == [0, 0, 1, 0]


def test_a_zero_range_bar_at_a_normal_move_is_not_a_lock():
    """An illiquid no-trade day is not a ceiling lock and must not be labelled one."""
    frame = _kr_frame([100, 101, 101, 102], zero_range_at={2})
    assert _kr_limit_flags(frame).tolist() == [0, 0, 0, 0]


def test_limit_down_lock_is_labelled_negative():
    frame = _kr_frame([100, 100, 70, 71], zero_range_at={2})
    assert _kr_limit_flags(frame).tolist() == [0, 0, -1, 0]


def test_the_15_percent_cap_applies_before_2015_06_15():
    frame = _kr_frame([100, 100, 115, 116], zero_range_at={2})
    frame["session"] = pd.to_datetime(
        ["2015-05-04", "2015-05-06", "2015-05-07", "2015-05-08"]
    )
    assert _kr_limit_flags(frame).tolist() == [0, 0, 1, 0]
    frame["session"] = pd.to_datetime(
        ["2020-05-04", "2020-05-06", "2020-05-07", "2020-05-08"]
    )
    assert _kr_limit_flags(frame).tolist() == [0, 0, 0, 0]


def test_contiguity_flag_is_false_across_a_missing_session():
    calendar = pd.DatetimeIndex(pd.bdate_range("2020-01-02", periods=10))
    frame = pd.DataFrame({"session": calendar.delete(4)})
    marked = _mark_contiguity(frame, calendar=calendar)
    assert marked["contiguous_prev"].tolist() == [
        False,
        True,
        True,
        True,
        False,
        True,
        True,
        True,
        True,
    ]


def _synthetic_kr_bars(lock_event_bar: bool) -> SymbolBars:
    """One deterministic +12%/RSI>=75 spike with a controllable lock on it."""
    rng = np.random.default_rng(7)
    base = list(100 * np.exp(np.cumsum(rng.normal(0.0, 0.004, 200))))
    # a clean run-up that pushes RSI over 75, then the spike bar
    for step in range(30):
        base.append(base[-1] * (1.03 + 0.0001 * step))
    spike = base[-1] * 1.30 if lock_event_bar else base[-1] * 1.15
    base.append(spike)
    base.extend(base[-1] * (1 - 0.01 * k) for k in range(1, 45))

    zero_range = {len(base) - 45} if lock_event_bar else set()
    frame = _kr_frame(base, zero_range_at=zero_range)
    frame = _mark_contiguity(frame, calendar=pd.DatetimeIndex(frame["session"]))
    frame["limit_locked"] = _kr_limit_flags(frame)
    return SymbolBars(
        "kr", "000000", frame, 0, 0, group="code_suffix_0", segment="KOSPI"
    )


def test_a_ceiling_locked_event_bar_is_flagged_not_silently_traded():
    bars = _synthetic_kr_bars(lock_event_bar=True)
    events = [o for o in scan_symbol(bars).observations if o.kind == "event"]
    assert events, "fixture must produce an event"
    locked = [o for o in events if o.limit_locked == 1]
    assert locked, "the ceiling-locked spike must be detected"
    for observation in locked:
        for horizon in (7, 30):
            block = observation.forward[f"{BASIS_EVENT_CLOSE}:{horizon}"]
            assert block["trim_executable"] is False


def test_an_unlocked_event_bar_stays_executable():
    bars = _synthetic_kr_bars(lock_event_bar=False)
    events = [o for o in scan_symbol(bars).observations if o.kind == "event"]
    assert events
    for observation in events:
        assert observation.limit_locked == 0
        assert observation.forward[f"{BASIS_EVENT_CLOSE}:7"]["trim_executable"] is True


def test_locked_bars_are_removed_from_the_rebid_fill_window():
    bars = _synthetic_kr_bars(lock_event_bar=True)
    for observation in scan_symbol(bars).observations:
        for block in observation.forward.values():
            low_all = block["window_low_including_locked"]
            low_tradable = block["window_low"]
            assert low_tradable is None or low_all <= low_tradable


def test_both_execution_bases_are_recorded_for_every_observation():
    bars = _synthetic_kr_bars(lock_event_bar=False)
    for observation in scan_symbol(bars).observations:
        for horizon in (7, 30):
            assert f"{BASIS_EVENT_CLOSE}:{horizon}" in observation.forward
            assert f"{BASIS_NEXT_OPEN}:{horizon}" in observation.forward
        assert observation.gap_next_open is not None


def test_next_open_basis_prices_from_the_bar_after_the_event():
    bars = _synthetic_kr_bars(lock_event_bar=False)
    frame = bars.frame
    for observation in scan_symbol(bars).observations:
        i = observation.index
        block = observation.forward[f"{BASIS_NEXT_OPEN}:7"]
        assert block["p0"] == pytest.approx(float(frame["open"].iloc[i + 1]))
        assert block["exit_price"] == pytest.approx(float(frame["open"].iloc[i + 8]))
