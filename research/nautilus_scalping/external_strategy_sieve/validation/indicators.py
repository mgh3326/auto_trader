"""ROB-383 Phase 3 - pure clean-room technical indicators (stdlib only).

Each indicator is reimplemented from its public mathematical definition; no
external source is copied. Inputs are float series or ``families.Bar``
sequences; outputs align index-for-index with ``None`` during the warmup window.
"""

from __future__ import annotations

from collections.abc import Sequence

import families


def closes_of(bars: Sequence[families.Bar]) -> list[float]:
    return [b.close for b in bars]


def sma(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    k = 2.0 / (n + 1)
    e: float | None = None
    for i, v in enumerate(values):
        e = v if e is None else v * k + e * (1 - k)
        out[i] = e
    return out


def rolling_std(values: Sequence[float], n: int) -> list[float | None]:
    out: list[float | None] = [None] * len(values)
    for i in range(len(values)):
        if i >= n - 1:
            window = values[i - n + 1 : i + 1]
            mean = sum(window) / n
            var = sum((x - mean) ** 2 for x in window) / n
            out[i] = var**0.5
    return out


def true_range(bars: Sequence[families.Bar]) -> list[float]:
    out: list[float] = []
    prev_close: float | None = None
    for bar in bars:
        if prev_close is None:
            tr = bar.high - bar.low
        else:
            tr = max(
                bar.high - bar.low,
                abs(bar.high - prev_close),
                abs(bar.low - prev_close),
            )
        out.append(tr)
        prev_close = bar.close
    return out


def atr(bars: Sequence[families.Bar], n: int) -> list[float | None]:
    """Wilder's RMA of true range, seeded with the mean of the first n TRs."""
    tr = true_range(bars)
    out: list[float | None] = [None] * len(bars)
    if len(bars) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(bars)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def rsi(closes: Sequence[float], n: int) -> list[float | None]:
    """Wilder's RSI."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        delta = closes[i] - closes[i - 1]
        gains += max(delta, 0.0)
        losses += max(-delta, 0.0)
    avg_gain, avg_loss = gains / n, losses / n
    out[n] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
    for i in range(n + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        avg_gain = (avg_gain * (n - 1) + max(delta, 0.0)) / n
        avg_loss = (avg_loss * (n - 1) + max(-delta, 0.0)) / n
        out[i] = (
            100.0
            if avg_loss == 0
            else 100.0 - 100.0 / (1 + avg_gain / avg_loss)
        )
    return out


def bollinger(
    closes: Sequence[float], n: int, k: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = sma(closes, n)
    sd = rolling_std(closes, n)
    upper: list[float | None] = [None] * len(closes)
    lower: list[float | None] = [None] * len(closes)
    for i in range(len(closes)):
        if mid[i] is not None and sd[i] is not None:
            upper[i] = mid[i] + k * sd[i]
            lower[i] = mid[i] - k * sd[i]
    return mid, upper, lower


def keltner(
    bars: Sequence[families.Bar], n: int, mult: float
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    mid = ema(closes_of(bars), n)
    a = atr(bars, n)
    upper: list[float | None] = [None] * len(bars)
    lower: list[float | None] = [None] * len(bars)
    for i in range(len(bars)):
        if mid[i] is not None and a[i] is not None:
            upper[i] = mid[i] + mult * a[i]
            lower[i] = mid[i] - mult * a[i]
    return mid, upper, lower
