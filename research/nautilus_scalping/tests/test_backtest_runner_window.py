"""ROB-339 PR3 — backtest_runner Nautilus-path window constraint (pure helpers).

backtest_runner stays venv-free at import (stdlib + validated_gate only), so the
window helpers are stdlib and unit-testable without Nautilus. The end-to-end
engine run (which applies the filter before engine.add_data) is verified at smoke
— no research venv in this environment.
"""

from __future__ import annotations

from collections import namedtuple

from backtest_runner import _filter_ticks_window, _window_bounds_ns
from discovery.data import window_bounds_ns

_Tick = namedtuple("_Tick", "ts_event")


def test_window_bounds_matches_discovery_path() -> None:
    # the Nautilus-path bounds must equal the discovery-path bounds (one window meaning)
    for wf, wt in [
        ("2026-03-01", "2026-03-05"),
        ("", ""),
        ("2026-03-02", ""),
        ("", "2026-04-01"),
    ]:
        assert _window_bounds_ns(wf, wt) == window_bounds_ns(wf, wt)


def test_filter_ticks_window_half_open() -> None:
    ticks = [_Tick(10), _Tick(20), _Tick(30)]
    assert _filter_ticks_window(ticks, 20, 30) == [_Tick(20)]  # [lo, hi): 30 excluded
    assert [t.ts_event for t in _filter_ticks_window(ticks, 15, None)] == [20, 30]
    assert [t.ts_event for t in _filter_ticks_window(ticks, None, 25)] == [10, 20]


def test_filter_ticks_window_none_bounds_passthrough() -> None:
    ticks = [_Tick(10), _Tick(20)]
    assert _filter_ticks_window(ticks, None, None) is ticks  # no-op, same object
