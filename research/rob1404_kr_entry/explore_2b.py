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

import math
from datetime import time
from typing import Any

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.core import decision_index, tick_floor
from research.rob1383_buy_drought.prices import KST
from research.rob1404_kr_entry.clock import (
    SessionCalendar,
    bar_state,
    next_executable_price,
)
from research.rob1404_kr_entry.methods import valid_price

ATR_PERIOD = 14
ATR_MULT = 0.5
REBOUND_CAP_ATR = 0.1
REBOUND_LAST_BAR_END = time(14, 0)
DEEP_LIMIT_PCT = (3.0, 12.0)  # buy.deep_limit_pct_range [-12, -3]
MATURITY_HORIZONS = (5, 20)


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


def maturity(
    bars: pd.DataFrame, signal_at: Any, cal: SessionCalendar
) -> dict[str, Any]:
    """``D0 + k`` maturity on the common calendar, the rule of
    ``outcomes.evaluate``: ``D0`` and the target are calendar positions, and a
    stock row missing on either date is ``data_missing`` (not the next row);
    a date past the calendar or past the stock's last cached bar is
    ``immature``; a target close that is not finite and > 0 is ``data_missing``."""

    i0 = cal.d0(signal_at)
    out: dict[str, Any] = {"d0_session": None if i0 is None else str(cal.dates[i0])}
    d0_state = None if i0 is None else bar_state(bars, cal.dates[i0])[1]
    for k in MATURITY_HORIZONS:
        if i0 is None or i0 + k >= len(cal.dates):
            status, why = "immature", "beyond_calendar"
        elif d0_state != "ok":
            status = "immature" if d0_state == "beyond_data_end" else "data_missing"
            why = f"d0_{d0_state}"
        else:
            pos, state = bar_state(bars, cal.dates[i0 + k])
            if pos is None:
                status = "immature" if state == "beyond_data_end" else "data_missing"
                why = f"target_{state}"
            elif not valid_price(float(bars["close"].iloc[pos])):
                status, why = "data_missing", "target_nonfinite_close"
            else:
                status, why = "ok", ""
        out[f"maturity_d{k}"] = status
        out[f"maturity_d{k}_reason"] = why
        out[f"matured_d{k}"] = status == "ok"
    return out


def candidate_row(
    bars: pd.DataFrame,
    hourly: pd.DataFrame | None,
    signal_at: Any,
    anchor: float,
    *,
    cal: SessionCalendar,
) -> dict[str, Any]:
    # Maturity first: every row carries it, including rows that stop early
    # (no pre-signal bar, no D0 bar, no ATR, non-positive limit).
    out: dict[str, Any] = maturity(bars, signal_at, cal)
    t = decision_index(bars, signal_at)  # last completed session at the signal
    i0 = cal.d0(signal_at)
    d0_date = None if i0 is None else cal.dates[i0]
    d0_ok = d0_date is not None and bar_state(bars, d0_date)[1] == "ok"
    out["t_pos_ok"] = t is not None and d0_ok
    if not out["t_pos_ok"]:
        return out
    h = bars["high"].to_numpy(float)[: t + 1]
    lo = bars["low"].to_numpy(float)[: t + 1]
    c = bars["close"].to_numpy(float)[: t + 1]
    # A cache read does not validate prices. Check every consumed daily price
    # before ATR, tick rounding or division; this never changes maturity.
    if not all(valid_price(float(p)) for values in (h, lo, c) for p in values):
        out.update(atr_available=False, atr_status="data_missing")
        return out
    atr = wilder_atr(h, lo, c)
    out["atr_available"] = atr is not None and math.isfinite(atr) and atr >= 0
    out["atr_status"] = "ok" if out["atr_available"] else "unavailable"
    if not out["atr_available"]:
        return out
    close_t, high_t = float(c[-1]), float(h[-1])
    raw_limit = close_t - ATR_MULT * atr
    out["atr_limit_nonpositive"] = not valid_price(raw_limit)
    if not valid_price(raw_limit):
        return out  # 0.5 ATR >= the close: no valid limit (counted, not raised)
    limit = tick_floor("kr", raw_limit)
    if not valid_price(limit):
        out["atr_limit_nonpositive"] = True
        return out
    nep = next_executable_price(bars, hourly, signal_at)
    anchor_ok = valid_price(anchor)
    nep_ok = nep is not None and valid_price(nep.price)
    dist_close = (close_t - limit) / close_t * 100.0
    out.update(
        {
            "atr_pct_of_close": atr / close_t * 100.0,
            "atr_limit_dist_below_close_pct": dist_close,
            "anchor_price_status": "ok" if anchor_ok else "data_missing",
            "nep_price_status": "ok" if nep_ok else "data_missing",
            "atr_limit_vs_anchor_pct": (limit / anchor - 1.0) * 100.0
            if anchor_ok
            else None,
            "atr_limit_vs_nep_pct": (limit / nep.price - 1.0) * 100.0
            if nep_ok
            else None,
            "anchor_dist_below_close_pct": (close_t - anchor) / close_t * 100.0
            if anchor_ok
            else None,
            "atr_limit_shallower_than_deep_min": dist_close < DEEP_LIMIT_PCT[0],
            "atr_limit_deeper_than_deep_max": dist_close > DEEP_LIMIT_PCT[1],
        }
    )
    trigger = None
    out["rebound_price_status"] = "ok"
    if hourly is not None:
        day = hourly[hourly["session_date"] == d0_date]
        sig = pd.Timestamp(signal_at)
        day = day[
            (day["start_ts"] >= sig)
            & day["end_ts"].map(
                lambda e: pd.Timestamp(e).tz_convert(KST).time() <= REBOUND_LAST_BAR_END
            )
        ]
        usable = day["close"].map(lambda p: valid_price(float(p)))
        out["rebound_hours_observable"] = int(usable.sum())
        if not usable.all():
            # An invalid close cannot prove absence, or let a later bar become
            # the first trigger. Only rebound statistics are unavailable.
            out.update(rebound_price_status="data_missing", rebound_triggered=None)
            return out
        for _, bar in day.iterrows():
            if float(bar["close"]) > high_t:
                trigger = bar
                break
    out["rebound_triggered"] = trigger is not None
    if trigger is not None:
        cap = float(trigger["close"]) + REBOUND_CAP_ATR * atr
        if not valid_price(cap):
            out["rebound_price_status"] = "data_missing"
            return out
        out["rebound_cap_vs_close_t_pct"] = (cap / close_t - 1.0) * 100.0
        out["rebound_cap_vs_anchor_pct"] = (
            (cap / anchor - 1.0) * 100.0 if anchor_ok else None
        )
    return out
