"""ROB-339 (D4) — the discovery window is a REAL data constraint.

Proves read_ticks excludes out-of-window rows via a pyarrow predicate-pushdown
filter on ts_event (not metadata, not a post-hoc slice the caller forgets to
apply), plus the date->ns bounds helper and tick->bar aggregation.
"""

from __future__ import annotations

import pandas as pd
from discovery.data import aggregate_to_bars, read_ticks, window_bounds_ns

_NS_DAY1 = pd.Timestamp("2026-03-01", tz="UTC").value
_NS_DAY2 = pd.Timestamp("2026-03-02", tz="UTC").value
_NS_DAY3 = pd.Timestamp("2026-03-03", tz="UTC").value


def _write_ticks(path) -> None:
    # 2 ticks on day1, 3 on day2, 1 on day3
    rows = [
        (_NS_DAY1, 0.50, 100.0),
        (_NS_DAY1 + 30_000_000_000, 0.51, 50.0),
        (_NS_DAY2, 0.60, 10.0),
        (_NS_DAY2 + 10_000_000_000, 0.62, 20.0),
        (_NS_DAY2 + 20_000_000_000, 0.61, 30.0),
        (_NS_DAY3, 0.70, 5.0),
    ]
    pd.DataFrame(rows, columns=["ts_event", "price", "size"]).to_parquet(path)


def test_read_ticks_window_excludes_out_of_window_rows(tmp_path) -> None:
    p = tmp_path / "ticks.parquet"
    _write_ticks(p)
    df = read_ticks(p, ts_from=_NS_DAY2, ts_to=_NS_DAY3)
    assert len(df) == 3  # only the day2 ticks
    assert df["ts_event"].min() >= _NS_DAY2
    assert df["ts_event"].max() < _NS_DAY3


def test_read_ticks_no_bounds_returns_all(tmp_path) -> None:
    p = tmp_path / "ticks.parquet"
    _write_ticks(p)
    assert len(read_ticks(p, ts_from=None, ts_to=None)) == 6


def test_window_bounds_ns_date_inclusive_to() -> None:
    lo, hi = window_bounds_ns("2026-03-02", "2026-03-02")
    assert lo == _NS_DAY2
    assert hi == _NS_DAY3  # 'to' date is inclusive -> half-open up to next midnight


def test_window_bounds_ns_blank_is_none() -> None:
    assert window_bounds_ns("", "") == (None, None)


def test_aggregate_to_bars_ohlcv() -> None:
    base = _NS_DAY2
    ticks = pd.DataFrame(
        {
            "ts_event": [base, base + 10_000_000_000, base + 20_000_000_000],
            "price": [0.60, 0.62, 0.61],
            "size": [10.0, 20.0, 30.0],
        }
    )
    bars = aggregate_to_bars(ticks, freq="1min")
    assert len(bars) == 1
    bar = bars.iloc[0]
    assert bar["open"] == 0.60
    assert bar["high"] == 0.62
    assert bar["low"] == 0.60
    assert bar["close"] == 0.61
    assert bar["volume"] == 60.0
