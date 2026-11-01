"""Labels per hypothesis. Unknown stays NaN, never 0 (rules §2.5).

Terminal rule (manifest v2): a listing that ends inside the window is not
censored, because dropping it would rebuild survivorship. It exits at its last
membership close, provided the terminal event was known by the end of the label
window. That close is the base assumption; ``simulator`` also runs a write-off
stress, since U2 asserts no payout amount.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from research.kr_boosting.config import ADMIT_HORIZON, TOUCH_WINDOW, XRET_HOLD
from research.kr_boosting.panel import (
    NAT,
    STATE_INVALID,
    STATE_NO_TRADE,
    STATE_VALID,
    Panel,
    kst_us,
)


def _terminal_exit_close(panel: Panel, li: int, end: int) -> float:
    """Last close if listing ``li`` ended before ``end`` with a known event, else NaN."""

    term = int(panel.terminal_idx[li])
    if term < 0 or term >= end or end >= len(panel.calendar):
        return np.nan
    known = int(panel.terminal_available_at[li])
    if known == NAT or known > kst_us(panel.calendar[end], 16, 0):
        return np.nan
    return float(panel.close[li, term])


def admit_label(panel: Panel, li: int, t: int) -> float:
    """t+1 open -> t+5 close, gross. NaN when either price is unknown."""

    n = len(panel.calendar)
    entry, end = t + 1, t + ADMIT_HORIZON
    if end >= n or panel.state[li, entry] != STATE_VALID:
        return np.nan
    open_price = panel.open[li, entry]
    if panel.state[li, end] == STATE_VALID:
        return float(panel.close[li, end] / open_price - 1.0)
    exit_close = _terminal_exit_close(panel, li, end)
    return float(exit_close / open_price - 1.0) if exit_close == exit_close else np.nan


@dataclass(frozen=True, slots=True)
class Touch:
    touched: float  # 1, 0 or NaN
    fill_idx: int  # -1 when not touched
    entry: float  # gap fill at the open, else the level


def touch_label(panel: Panel, li: int, t: int, level: float) -> Touch:
    """Whether ``low <= level`` in t+1..t+5.

    An ``invalid_ohlc`` session is unobserved, so the window is UNKNOWN (rules
    §2.5). A ``no_trade`` session is observed with no print. Once a listing has
    ended no trade can happen, so an untouched window closed by a terminal event
    is 0; a window that merely runs past the data is UNKNOWN.
    """

    n = len(panel.calendar)
    if t + TOUCH_WINDOW >= n:
        return Touch(np.nan, -1, np.nan)
    for i in range(t + 1, t + TOUCH_WINDOW + 1):
        state = panel.state[li, i]
        if state == STATE_INVALID:
            return Touch(np.nan, -1, np.nan)
        if state == STATE_NO_TRADE:
            continue
        if state == STATE_VALID:
            if panel.low[li, i] <= level:
                open_price = float(panel.open[li, i])
                return Touch(1.0, i, min(open_price, level))
            continue
        # Absent: the listing has ended, or never began here. The zero needs the
        # ending known by the window's end, as _terminal_exit_close does.
        known = int(panel.terminal_available_at[li])
        if (
            int(panel.terminal_idx[li]) >= 0
            and i > int(panel.terminal_idx[li])
            and known != NAT
            and known <= kst_us(panel.calendar[t + TOUCH_WINDOW], 16, 0)
        ):
            return Touch(0.0, -1, np.nan)
        return Touch(np.nan, -1, np.nan)
    return Touch(0.0, -1, np.nan)


def market_log_return(market_ret: np.ndarray, start: int, end: int) -> float:
    """Equal-weight market log return over sessions start+1..end."""

    return float(np.nansum(market_ret[start + 1 : end + 1]))


def xret_label(panel: Panel, li: int, touch: Touch, market_ret: np.ndarray) -> float:
    """Return over the 20 sessions after the touch fill, minus the same market's."""

    if touch.touched != 1.0:
        return np.nan
    n = len(panel.calendar)
    end = touch.fill_idx + XRET_HOLD
    if end >= n:
        return np.nan
    if panel.state[li, end] == STATE_VALID:
        exit_close, bench_end = float(panel.close[li, end]), end
    else:
        exit_close = _terminal_exit_close(panel, li, end)
        if exit_close != exit_close:
            return np.nan
        bench_end = int(panel.terminal_idx[li])
    bench = np.expm1(market_log_return(market_ret, touch.fill_idx, bench_end))
    return float(exit_close / touch.entry - 1.0 - bench)
