"""The frozen D3 signal as "the current gate", vectorised over the panel.

For a decision after the close of session ``t`` (D3's session ``S = t+1``) a
listing is eligible when, over its contiguous valid bars ending at ``t``:

* at least 120 bars exist (D3 ``decision_index >= 120``);
* Wilder RSI-14 rounded to 4 places is below 45;
* a cluster with both fib and Bollinger sources sits within -8%..-3% of the close.

The cluster decision itself goes through D3's own ``cluster_levels`` /
``choose_l2``; only the prefilter and the RSI run on floats. A test proves the
output equals ``PortfolioEngine._signal_for_session`` on fixture bars.
Bars carry no analyst data, so this is the bar-reproducible subset of the live
gate (no upside gate).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import numpy as np

from research.kr_boosting.panel import STATE_VALID, Panel
from research.kr_corpus.d3_engine.constants import CONFLUENCE_TOLERANCE
from research.kr_corpus.d3_engine.indicators import (
    FIB_SUPPORT_RATIOS,
    bollinger_bands,
    fib_levels,
)
from research.kr_corpus.d3_engine.signals import (
    PriceLevel,
    choose_l2,
    cluster_levels,
    signal_is_eligible,
    support_distance,
)

FIB_WINDOW = 120
RSI_PERIOD = 14
_RSI_PREFILTER = 45.01
_FIB_RATIOS = np.array([float(r) for r in FIB_SUPPORT_RATIOS])


@dataclass(frozen=True, slots=True)
class Signal:
    li: int
    t: int
    rsi: float
    l2_price: Decimal
    l2_distance: float
    fib_nearest: Decimal | None  # nearest fib level in -8%..-0.5%


def wilder_rsi(closes: np.ndarray, period: int = RSI_PERIOD) -> np.ndarray:
    """Same recursion as D3 ``rsi_wilder``, on floats."""

    out = np.full(len(closes), np.nan)
    if len(closes) < period + 1:
        return out
    delta = np.diff(closes)
    gains = np.maximum(delta, 0.0)
    losses = np.maximum(-delta, 0.0)
    avg_gain = gains[:period].mean()
    avg_loss = losses[:period].mean()

    def rsi(g: float, li: float) -> float:
        if li == 0:
            return 100.0 if g > 0 else 50.0
        if g == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + g / li)

    out[period] = rsi(avg_gain, avg_loss)
    for i in range(period, len(delta)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        out[i + 1] = rsi(avg_gain, avg_loss)
    return out


def _segments(valid: np.ndarray) -> list[tuple[int, int]]:
    """Half-open runs of contiguous valid sessions."""

    edges = np.flatnonzero(np.diff(np.concatenate([[0], valid.astype(int), [0]])))
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2], strict=True)]


def _segment_signals(
    panel: Panel, li: int, start: int, stop: int, *, first_t: int, last_t: int
) -> list[Signal]:
    n = stop - start
    if n < FIB_WINDOW:
        return []
    closes = panel.close[li, start:stop]
    highs = panel.high[li, start:stop]
    lows = panel.low[li, start:stop]
    rsi = wilder_rsi(closes)

    win = np.lib.stride_tricks.sliding_window_view
    hi = win(highs, FIB_WINDOW).max(axis=1)
    lo = win(lows, FIB_WINDOW).min(axis=1)
    c20 = win(closes, 20)
    bb = c20.mean(axis=1) - 2.0 * c20.std(axis=1)  # ddof=0, as D3

    out: list[Signal] = []
    for i in range(FIB_WINDOW - 1, n):
        t = start + i
        if t < first_t or t > last_t:
            continue
        r = rsi[i]
        if not (r < _RSI_PREFILTER):
            continue
        close = closes[i]
        lower = bb[i - 19]
        dist = lower / close - 1.0
        if not -0.095 <= dist <= -0.015:
            continue
        h, w = hi[i - FIB_WINDOW + 1], lo[i - FIB_WINDOW + 1]
        fibs = w + _FIB_RATIOS * (h - w)
        if np.abs(fibs - lower).min() / close > float(CONFLUENCE_TOLERANCE) + 0.001:
            continue
        signal = _exact_signal(
            panel, li, t, closes[: i + 1], Decimal(str(round(r, 4))), w, h
        )
        if signal is not None:
            out.append(signal)
    return out


def _exact_signal(
    panel: Panel,
    li: int,
    t: int,
    closes: np.ndarray,
    rsi: Decimal,
    low: float,
    high: float,
) -> Signal | None:
    close = Decimal(int(closes[-1]))
    bands = bollinger_bands([Decimal(int(c)) for c in closes[-20:]])
    fibs = fib_levels(Decimal(int(low)), Decimal(int(high)))
    levels = [
        PriceLevel(price, "fib_family", f"fib_{ratio}") for ratio, price in fibs.items()
    ]
    levels.append(PriceLevel(bands.lower, "bb_lower", "bb_lower"))
    clusters = cluster_levels(levels, close=close)
    if not signal_is_eligible(rsi=rsi, clusters=clusters, close=close):
        return None
    l2 = choose_l2(clusters, close=close)
    assert l2 is not None
    near = [
        p
        for p in fibs.values()
        if Decimal("-0.08") <= support_distance(p, close) <= Decimal("-0.005")
    ]
    return Signal(
        li=li,
        t=t,
        rsi=float(rsi),
        l2_price=l2.representative,
        l2_distance=float(support_distance(l2.representative, close)),
        fib_nearest=max(near) if near else None,
    )


def d3_signals(
    panel: Panel, *, first_t: int = 0, last_t: int | None = None
) -> list[Signal]:
    """Every eligible (listing, t), sorted by t then listing."""

    last = panel.shape[1] - 1 if last_t is None else last_t
    out: list[Signal] = []
    for li in range(panel.shape[0]):
        for start, stop in _segments(panel.state[li] == STATE_VALID):
            out.extend(
                _segment_signals(panel, li, start, stop, first_t=first_t, last_t=last)
            )
    out.sort(key=lambda s: (s.t, s.li))
    return out
