"""Forward scoring for the screener bakeoff.

Entry = decision-date close.  Only bars strictly AFTER the decision date are
used for the outcome; the entry bar itself is never part of MFE/MAE.

Survivorship handling (stated, not hidden):
  * ``status="full"``    — all ``h`` forward bars exist.
  * ``status="truncated"`` — the symbol stops printing bars before ``h``
    sessions elapse while the market calendar continues (delisting, halt,
    or a coverage hole).  The return is measured to the LAST available bar
    and the row is flagged.  Headline stats include truncated rows; the
    ``strict`` sensitivity column drops them.
  * ``status="missing"``  — no forward bar at all, or no entry bar.  Never
    scored, always counted.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Outcome:
    status: str
    entry: float | None
    ret: float | None
    mfe: float | None
    mae: float | None
    ret_hl_mfe: float | None
    ret_hl_mae: float | None
    bars_used: int


_MISSING = Outcome("missing", None, None, None, None, None, None, 0)


def score(panel, symbol: str, day: dt.date, horizon: int) -> Outcome:
    idx = panel.index_of(symbol, day)
    if idx is None:
        return _MISSING
    closes = panel.close[symbol]
    entry = float(closes[idx])
    if entry <= 0:
        return _MISSING
    last = min(idx + horizon, closes.size - 1)
    if last <= idx:
        return _MISSING
    fwd = closes[idx + 1 : last + 1]
    status = "full" if last == idx + horizon else "truncated"
    ret = float(fwd[-1] / entry - 1.0)
    mfe = float(np.max(fwd) / entry - 1.0)
    mae = float(np.min(fwd) / entry - 1.0)
    if panel.has_intraday_range:
        hi = panel.high[symbol][idx + 1 : last + 1]
        lo = panel.low[symbol][idx + 1 : last + 1]
        hl_mfe = float(np.max(hi) / entry - 1.0)
        hl_mae = float(np.min(lo) / entry - 1.0)
    else:
        hl_mfe = hl_mae = None
    return Outcome(status, entry, ret, mfe, mae, hl_mfe, hl_mae, int(last - idx))


def summarise(returns: np.ndarray) -> dict:
    """Mean / median / win-rate / dispersion for a return vector."""
    clean = returns[~np.isnan(returns)]
    if clean.size == 0:
        return {
            "n": 0,
            "mean": None,
            "median": None,
            "win_rate": None,
            "std": None,
            "p10": None,
            "p90": None,
            "t_stat": None,
        }
    mean = float(clean.mean())
    std = float(clean.std(ddof=1)) if clean.size > 1 else float("nan")
    t_stat = (
        float(mean / (std / np.sqrt(clean.size)))
        if clean.size > 1 and std > 0
        else None
    )
    return {
        "n": int(clean.size),
        "mean": mean,
        "median": float(np.median(clean)),
        "win_rate": float((clean > 0).mean()),
        "std": std,
        "p10": float(np.percentile(clean, 10)),
        "p90": float(np.percentile(clean, 90)),
        "t_stat": t_stat,
    }
