"""Part 2B draft-spec feasibility counts -- EXPLORATORY (탐색) ONLY.

Computes, on the current candidates, how often the *proposed* 2B arms would
collide with existing policy and how many candidates would mature -- never a
return or a fill-based performance number (2B is not evaluated here).

Proposed draft values (pre-fixable proposals, not validated optima):
``ATR_MULT = 0.5`` below the last completed close, rebound trigger = a
completed hourly close above the prior session high on a bar ending by 14:00
KST, rebound limit cap ``+ 0.1 ATR``.
"""

from __future__ import annotations

from datetime import time
from typing import Any

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.core import decision_index, tick_floor
from research.rob1383_buy_drought.prices import KST
from research.rob1404_kr_entry.clock import d0_index, next_executable_price

ATR_PERIOD = 14
ATR_MULT = 0.5
REBOUND_CAP_ATR = 0.1
REBOUND_LAST_BAR_END = time(14, 0)
DEEP_LIMIT_PCT = (3.0, 12.0)  # buy.deep_limit_pct_range [-12, -3]


def wilder_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ATR_PERIOD
) -> float | None:
    """Wilder ATR of the *last* bar given completed daily bars (oldest first)."""

    n = len(close)
    if n < period + 1:
        return None
    prev = close[:-1]
    tr = np.maximum.reduce(
        [high[1:] - low[1:], np.abs(high[1:] - prev), np.abs(low[1:] - prev)]
    )
    atr = float(tr[:period].mean())
    for x in tr[period:]:
        atr = (atr * (period - 1) + float(x)) / period
    return atr


def candidate_row(
    bars: pd.DataFrame, hourly: pd.DataFrame | None, signal_at: Any, anchor: float
) -> dict[str, Any]:
    t = decision_index(bars, signal_at)  # last completed session at the signal
    d0 = d0_index(bars, signal_at)
    out: dict[str, Any] = {"t_pos_ok": t is not None and d0 is not None}
    if t is None or d0 is None:
        return out
    h = bars["high"].to_numpy(float)[: t + 1]
    lo = bars["low"].to_numpy(float)[: t + 1]
    c = bars["close"].to_numpy(float)[: t + 1]
    atr = wilder_atr(h, lo, c)
    out["atr_available"] = atr is not None
    if atr is None:
        return out
    close_t, high_t = float(c[-1]), float(h[-1])
    limit = tick_floor("kr", close_t - ATR_MULT * atr)
    nep = next_executable_price(bars, hourly, signal_at)
    dist_close = (close_t - limit) / close_t * 100.0
    out.update(
        {
            "atr_pct_of_close": atr / close_t * 100.0,
            "atr_limit_dist_below_close_pct": dist_close,
            "atr_limit_vs_anchor_pct": (limit / anchor - 1.0) * 100.0,
            "atr_limit_vs_nep_pct": (limit / nep.price - 1.0) * 100.0 if nep else None,
            "anchor_dist_below_close_pct": (close_t - anchor) / close_t * 100.0,
            "atr_limit_shallower_than_deep_min": dist_close < DEEP_LIMIT_PCT[0],
            "atr_limit_deeper_than_deep_max": dist_close > DEEP_LIMIT_PCT[1],
        }
    )
    trigger = None
    if hourly is not None:
        day = hourly[hourly["session_date"] == bars["session_date"].iloc[d0]]
        sig = pd.Timestamp(signal_at)
        for _, bar in day.iterrows():
            end_kst = pd.Timestamp(bar["end_ts"]).tz_convert(KST).time()
            if end_kst > REBOUND_LAST_BAR_END:
                break
            if pd.Timestamp(bar["start_ts"]) < sig:
                continue  # a bar that began before the signal is not observable in full
            if float(bar["close"]) > high_t:
                trigger = bar
                break
        out["rebound_hours_observable"] = int(
            (
                (day["start_ts"] >= sig)
                & day["end_ts"].map(
                    lambda e: (
                        pd.Timestamp(e).tz_convert(KST).time() <= REBOUND_LAST_BAR_END
                    )
                )
            ).sum()
        )
    out["rebound_triggered"] = trigger is not None
    if trigger is not None:
        cap = float(trigger["close"]) + REBOUND_CAP_ATR * atr
        out["rebound_cap_vs_close_t_pct"] = (cap / close_t - 1.0) * 100.0
        out["rebound_cap_vs_anchor_pct"] = (cap / anchor - 1.0) * 100.0
    out["matured_d5"] = d0 + 5 < len(bars)
    out["matured_d20"] = d0 + 20 < len(bars)
    return out
