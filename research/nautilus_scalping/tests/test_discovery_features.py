"""ROB-339 — discovery feature engineering (pure pandas).

Backward returns/range/close-position/vol regime/time bucket as inputs; forward
returns (bps) as outcomes. Hand-computable on a tiny synthetic bar ramp.
"""

from __future__ import annotations

import pandas as pd
from discovery.features import add_features


def _bars() -> pd.DataFrame:
    dt = pd.date_range("2026-03-02 13:00", periods=6, freq="1min", tz="UTC")
    close = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    high = [c + 0.5 for c in close]
    low = [c - 0.5 for c in close]
    return pd.DataFrame(
        {
            "dt": dt,
            "open": close,
            "high": high,
            "low": low,
            "close": close,
            "volume": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
        }
    )


def test_backward_and_forward_returns_bps() -> None:
    f = add_features(_bars())
    assert round(f["ret_1m"].iloc[1], 4) == 100.0  # (101-100)/100*1e4
    assert round(f["fwd_ret_1m"].iloc[0], 4) == 100.0  # forward 1-bar return
    assert round(f["fwd_ret_3m"].iloc[0], 4) == 300.0  # (103-100)/100*1e4


def test_range_and_close_position() -> None:
    f = add_features(_bars())
    assert round(f["bar_range_bps"].iloc[0], 4) == 100.0  # (100.5-99.5)/100*1e4
    assert round(f["close_pos"].iloc[0], 4) == 0.5  # mid of [low, high]


def test_time_bucket_and_regime_columns_present() -> None:
    f = add_features(_bars())
    assert f["time_bucket"].iloc[0] == "us"  # 13:00 UTC
    for col in (
        "realized_vol_bps",
        "vol_bucket",
        "roll_high",
        "roll_low",
        "near_funding",
        "next_low",
        "next_high",
    ):
        assert col in f.columns


def test_next_bar_lookahead_columns() -> None:
    f = add_features(_bars())
    # next_low / next_high are the following bar's extremes (for maker fill est)
    assert round(f["next_low"].iloc[0], 4) == 100.5  # bar 1 low = 101-0.5
    assert round(f["next_high"].iloc[0], 4) == 101.5  # bar 1 high = 101+0.5
