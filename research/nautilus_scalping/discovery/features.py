"""ROB-339 — discovery feature engineering (pure pandas; no Nautilus).

Backward features (returns, range, close position, realized-vol regime, time
bucket, rolling sweep levels) are inputs; forward returns in bps are outcomes.
All vectorized; degenerate small inputs never raise (buckets fall back to normal).
"""

from __future__ import annotations

import pandas as pd

_BPS = 1e4


# Disjoint UTC-hour session buckets (KST = UTC+9).
def _time_bucket(hour: int) -> str:
    if 0 <= hour < 7:
        return "asia"  # KST 09:00-16:00 neighborhood
    if 7 <= hour < 13:
        return "eu"
    if 13 <= hour < 21:
        return "us"
    return "off"


# Binance USDⓈ-M funding settles 00:00 / 08:00 / 16:00 UTC.
_FUNDING_HOURS = (0, 8, 16)


def _vol_bucket(rv: pd.Series) -> pd.Series:
    valid = rv.dropna()
    out = pd.Series("normal", index=rv.index, dtype=object)
    if valid.nunique() < 3:
        return out
    lo_q, hi_q = valid.quantile(0.33), valid.quantile(0.66)
    out[rv <= lo_q] = "low"
    out[rv >= hi_q] = "high"
    out[rv.isna()] = "normal"
    return out


def add_features(
    bars: pd.DataFrame, *, vol_window: int = 60, roll_window: int = 20
) -> pd.DataFrame:
    """Return a copy of ``bars`` with discovery feature + forward-outcome columns."""
    f = bars.copy().reset_index(drop=True)
    close, high, low = f["close"], f["high"], f["low"]

    # backward returns (bps)
    for n in (1, 3, 5):
        f[f"ret_{n}m"] = close.pct_change(n) * _BPS
    # forward outcomes (bps)
    for n in (1, 3, 5):
        f[f"fwd_ret_{n}m"] = (close.shift(-n) / close - 1.0) * _BPS

    span = high - low
    f["bar_range_bps"] = span / close * _BPS
    f["close_pos"] = ((close - low) / span).where(span != 0, 0.5)

    f["realized_vol_bps"] = f["ret_1m"].rolling(vol_window, min_periods=2).std()
    f["vol_bucket"] = _vol_bucket(f["realized_vol_bps"])
    vol_mean = f["volume"].rolling(vol_window, min_periods=2).mean()
    vol_std = f["volume"].rolling(vol_window, min_periods=2).std()
    f["vol_z"] = ((f["volume"] - vol_mean) / vol_std).where(vol_std != 0, 0.0)

    # rolling sweep levels from PRIOR bars (exclude current via shift(1))
    f["roll_high"] = high.rolling(roll_window, min_periods=1).max().shift(1)
    f["roll_low"] = low.rolling(roll_window, min_periods=1).min().shift(1)

    dt = pd.to_datetime(f["dt"], utc=True)
    f["time_bucket"] = dt.dt.hour.map(_time_bucket)
    mins_from_funding = dt.dt.hour.isin(_FUNDING_HOURS) & (dt.dt.minute < 5)
    near_prev = dt.dt.hour.isin([h - 1 for h in _FUNDING_HOURS]) & (dt.dt.minute > 55)
    f["near_funding"] = mins_from_funding | near_prev

    # next-bar extremes for maker passive-fill estimation
    f["next_low"] = low.shift(-1)
    f["next_high"] = high.shift(-1)
    return f
