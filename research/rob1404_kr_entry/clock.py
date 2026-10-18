"""Common evaluation clock and look-ahead-free entry prices (pure, unit-tested).

Definitions (KRX regular session only -- public bars hold no NXT/after-market):

* ``D0`` = the first daily bar whose close is after ``signal_at``: the session
  in progress at the signal, or the next one (``core.forward_positions``).
  Every method of one candidate is evaluated at the close of ``D0 + h``.
* **Next executable price (NEP)** -- the first KRX price an order sent at
  ``signal_at`` could trade at, using only bars that *start after* it:
  1. open of the first hourly bar of ``D0`` with ``start_ts > signal_at``;
  2. else, if ``signal_at`` is before the 15:20 KST closing-auction cutoff of
     ``D0`` (inside the last hourly bar), the ``D0`` closing-auction price
     (= the daily close, not yet printed at ``signal_at``);
  3. else the open of the next session (first hourly open, daily open fallback).
  A signal before ``D0``'s open buys ``D0``'s open. The hour containing the
  signal never supplies a price (its open/low precede or straddle the signal).
* **Next-day open** -- open of the first session whose KST date is after the
  signal's KST date (for an evening/NXT-time signal this equals NEP).
* ``price_at(ts)`` -- an *executable* price stamped at ``ts``: open of an hourly
  bar starting exactly at ``ts``, the daily open when ``ts`` is the session
  open, the daily close when ``ts`` is the session close; otherwise ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time

import numpy as np
import pandas as pd

from research.rob1383_buy_drought.core import forward_positions
from research.rob1383_buy_drought.prices import KST

CLOSING_AUCTION_CUTOFF = time(15, 20)


@dataclass(frozen=True, slots=True)
class Entry:
    price: float
    at: pd.Timestamp  # when the price prints (UTC)
    pos: int  # daily bar position of the entry session
    basis: str


def d0_index(bars: pd.DataFrame, signal_at: datetime) -> int | None:
    pos = forward_positions(bars, signal_at, 1)
    return pos[0] if pos else None


def _session_hours(hourly: pd.DataFrame | None, session_date) -> pd.DataFrame:
    if hourly is None:
        return pd.DataFrame(columns=["start_ts", "open"])
    return hourly[hourly["session_date"] == session_date]


def _session_open(bars: pd.DataFrame, hourly: pd.DataFrame | None, pos: int) -> Entry:
    day = _session_hours(hourly, bars["session_date"].iloc[pos])
    open_ts = pd.Timestamp(bars["open_ts"].iloc[pos])
    if len(day) and pd.Timestamp(day["start_ts"].iloc[0]) == open_ts:
        return Entry(float(day["open"].iloc[0]), open_ts, pos, "session_open_hourly")
    return Entry(float(bars["open"].iloc[pos]), open_ts, pos, "session_open_daily")


def next_executable_price(
    bars: pd.DataFrame, hourly: pd.DataFrame | None, signal_at: datetime
) -> Entry | None:
    d0 = d0_index(bars, signal_at)
    if d0 is None:
        return None
    t = pd.Timestamp(signal_at)
    if t < pd.Timestamp(bars["open_ts"].iloc[d0]):
        return _session_open(bars, hourly, d0)
    day = _session_hours(hourly, bars["session_date"].iloc[d0])
    later = day[day["start_ts"] > t]
    if len(later):
        return Entry(
            float(later["open"].iloc[0]),
            pd.Timestamp(later["start_ts"].iloc[0]),
            d0,
            "next_hourly_open",
        )
    if t.tz_convert(KST).time() < CLOSING_AUCTION_CUTOFF:
        return Entry(
            float(bars["close"].iloc[d0]),
            pd.Timestamp(bars["close_ts"].iloc[d0]),
            d0,
            "closing_auction",
        )
    if d0 + 1 >= len(bars):
        return None
    return _session_open(bars, hourly, d0 + 1)


def next_day_open(
    bars: pd.DataFrame, hourly: pd.DataFrame | None, signal_at: datetime
) -> Entry | None:
    signal_day = pd.Timestamp(signal_at).tz_convert(KST).date()
    later = np.flatnonzero(
        np.array([d > signal_day for d in bars["session_date"]], dtype=bool)
    )
    if len(later) == 0:
        return None
    return _session_open(bars, hourly, int(later[0]))


def price_at(
    bars: pd.DataFrame, hourly: pd.DataFrame | None, ts: pd.Timestamp
) -> float | None:
    """Executable price printed at ``ts`` (see module doc), else ``None``."""

    ts = pd.Timestamp(ts)
    if hourly is not None:
        hit = hourly[hourly["start_ts"] == ts]
        if len(hit):
            return float(hit["open"].iloc[0])
    opens = bars[bars["open_ts"] == ts]
    if len(opens):
        return float(opens["open"].iloc[0])
    closes = bars[bars["close_ts"] == ts]
    if len(closes):
        return float(closes["close"].iloc[0])
    return None


def close_on(bars: pd.DataFrame, session_date) -> float | None:
    hit = bars[bars["session_date"] == session_date]
    return float(hit["close"].iloc[0]) if len(hit) else None


def first_touch_hour(
    hourly: pd.DataFrame | None,
    session_date,
    limit: float,
    start: pd.Timestamp | None = None,
    end: pd.Timestamp | None = None,
) -> pd.Timestamp | None:
    """Start of the first hourly bar of the session that *starts* at/after
    ``start`` (the hour containing an order never supplies its fill) and
    before ``end``, whose low reaches ``limit``; ``None`` if no hour shows it."""

    day = _session_hours(hourly, session_date)
    if len(day) == 0:
        return None
    keep = np.ones(len(day), dtype=bool)
    if start is not None:
        keep &= (day["start_ts"] >= start).to_numpy()
    if end is not None:
        keep &= (day["start_ts"] < end).to_numpy()
    hit = day[keep & (day["low"].to_numpy(float) <= limit)]
    return pd.Timestamp(hit["start_ts"].iloc[0]) if len(hit) else None
