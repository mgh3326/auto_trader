"""ROB-382 — pure-stdlib technical indicators for faithful external-strategy ports.

These re-express the talib / qtpylib / pandas_ta primitives the strat.ninja strategies
use, in dependency-free Python, so we can port ONLY the signal logic (no freqtrade /
pandas / talib runtime import — repo boundary). Each function returns a list aligned to
the input length, with ``float('nan')`` during the warmup window (mirrors pandas columns
so causal indexing and lookahead-avoidance are explicit).

Semantics matched (good enough for a GROSS sign screen; not bit-exact to talib):
  * ema    — SMA-seeded, alpha = 2/(n+1) (talib EMA)
  * rsi/atr — Wilder smoothing (talib RSI/ATR)
  * bollinger — sample std (ddof=1), matching pandas .std() used by ClucHAnix/VWAP
  * ichimoku — senkou spans forward-displaced (causal: span at t derives from t-displacement)
  * informative merge — last FULLY-CLOSED higher-tf bar as of base ts (lookahead-safe)
"""
from __future__ import annotations

import math
from collections.abc import Sequence

NAN = float("nan")


def _valid(x: float) -> bool:
    return x == x and x not in (float("inf"), float("-inf"))  # noqa: PLR0124 (nan check)


# --------------------------------------------------------------------------- #
# Moving averages
# --------------------------------------------------------------------------- #
def sma(xs: Sequence[float], n: int) -> list[float]:
    out = [NAN] * len(xs)
    if n <= 0:
        return out
    run = 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def ema(xs: Sequence[float], n: int) -> list[float]:
    out = [NAN] * len(xs)
    if n <= 0 or len(xs) < n:
        return out
    alpha = 2.0 / (n + 1)
    seed = sum(xs[:n]) / n  # talib seeds EMA with the SMA of the first n
    out[n - 1] = seed
    prev = seed
    for i in range(n, len(xs)):
        prev = alpha * xs[i] + (1 - alpha) * prev
        out[i] = prev
    return out


def wma(xs: Sequence[float], n: int) -> list[float]:
    out = [NAN] * len(xs)
    if n <= 0:
        return out
    denom = n * (n + 1) / 2.0
    for i in range(n - 1, len(xs)):
        acc = 0.0
        for k in range(n):
            acc += xs[i - n + 1 + k] * (k + 1)
        out[i] = acc / denom
    return out


def hma(xs: Sequence[float], n: int) -> list[float]:
    """Hull MA: WMA(2*WMA(n/2) - WMA(n), sqrt(n))."""
    if n <= 1:
        return list(xs)
    half = wma(xs, max(1, n // 2))
    full = wma(xs, n)
    raw = [
        (2 * h - f) if (_valid(h) and _valid(f)) else NAN
        for h, f in zip(half, full, strict=True)
    ]
    # WMA over the raw series, but the raw series has a NAN prefix; compute on the tail.
    sq = max(1, int(math.sqrt(n)))
    out = [NAN] * len(xs)
    for i in range(len(xs)):
        if i - sq + 1 < 0:
            continue
        window = raw[i - sq + 1 : i + 1]
        if any(not _valid(w) for w in window):
            continue
        denom = sq * (sq + 1) / 2.0
        out[i] = sum(w * (k + 1) for k, w in enumerate(window)) / denom
    return out


# --------------------------------------------------------------------------- #
# Oscillators
# --------------------------------------------------------------------------- #
def rsi(xs: Sequence[float], n: int = 14) -> list[float]:
    out = [NAN] * len(xs)
    if len(xs) <= n:
        return out
    gains = 0.0
    losses = 0.0
    for i in range(1, n + 1):
        d = xs[i] - xs[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g = gains / n
    avg_l = losses / n
    out[n] = 100.0 - 100.0 / (1 + (avg_g / avg_l)) if avg_l > 0 else 100.0
    for i in range(n + 1, len(xs)):
        d = xs[i] - xs[i - 1]
        avg_g = (avg_g * (n - 1) + max(d, 0.0)) / n
        avg_l = (avg_l * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 - 100.0 / (1 + (avg_g / avg_l)) if avg_l > 0 else 100.0
    return out


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], n: int = 14) -> list[float]:
    m = len(closes)
    out = [NAN] * m
    if m <= n:
        return out
    tr = [NAN] * m
    tr[0] = highs[0] - lows[0]
    for i in range(1, m):
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    prev = sum(tr[1 : n + 1]) / n
    out[n] = prev
    for i in range(n + 1, m):
        prev = (prev * (n - 1) + tr[i]) / n
        out[i] = prev
    return out


def rocr(xs: Sequence[float], n: int) -> list[float]:
    """Rate-of-change ratio: xs[i] / xs[i-n] (talib ROCR)."""
    out = [NAN] * len(xs)
    for i in range(n, len(xs)):
        if xs[i - n]:
            out[i] = xs[i] / xs[i - n]
    return out


def fisher_from_rsi(rsi_vals: Sequence[float]) -> list[float]:
    out = [NAN] * len(rsi_vals)
    for i, r in enumerate(rsi_vals):
        if not _valid(r):
            continue
        x = 0.1 * (r - 50.0)
        e = math.exp(2 * x)
        out[i] = (e - 1) / (e + 1)
    return out


def ewo(closes: Sequence[float], fast: int = 50, slow: int = 200) -> list[float]:
    ef = ema(closes, fast)
    es = ema(closes, slow)
    out = [NAN] * len(closes)
    for i in range(len(closes)):
        if _valid(ef[i]) and _valid(es[i]) and closes[i]:
            out[i] = (ef[i] - es[i]) / closes[i] * 100.0
    return out


def cti(closes: Sequence[float], n: int = 20) -> list[float]:
    """Correlation Trend Indicator (pandas_ta.cti): Pearson r of close vs a linear ramp."""
    out = [NAN] * len(closes)
    xs = list(range(n))
    mean_x = sum(xs) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    for i in range(n - 1, len(closes)):
        window = closes[i - n + 1 : i + 1]
        mean_y = sum(window) / n
        sxy = sum((xs[k] - mean_x) * (window[k] - mean_y) for k in range(n))
        syy = sum((y - mean_y) ** 2 for y in window)
        out[i] = sxy / math.sqrt(sxx * syy) if syy > 0 else 0.0
    return out


# --------------------------------------------------------------------------- #
# Volatility / volume bands
# --------------------------------------------------------------------------- #
def rolling_std(xs: Sequence[float], n: int, ddof: int = 1) -> list[float]:
    """Sample (ddof=1) rolling std, O(len) via running sum + sum-of-squares."""
    out = [NAN] * len(xs)
    denom = n - ddof
    if denom <= 0:
        return out
    s = 0.0
    ss = 0.0
    for i, x in enumerate(xs):
        s += x
        ss += x * x
        if i >= n:
            old = xs[i - n]
            s -= old
            ss -= old * old
        if i >= n - 1:
            var = (ss - s * s / n) / denom
            out[i] = math.sqrt(var) if var > 0 else 0.0
    return out


def bollinger(xs: Sequence[float], n: int, num_std: float, ddof: int = 1):
    """Return (mid, lower, upper). mid=SMA(n); bands = mid ± num_std*rolling_std."""
    mid = sma(xs, n)
    sd = rolling_std(xs, n, ddof=ddof)
    lower = [m - num_std * s if (_valid(m) and _valid(s)) else NAN for m, s in zip(mid, sd, strict=True)]
    upper = [m + num_std * s if (_valid(m) and _valid(s)) else NAN for m, s in zip(mid, sd, strict=True)]
    return mid, lower, upper


def rolling_vwap(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    volumes: Sequence[float],
    n: int = 20,
):
    """qtpylib.rolling_vwap: rolling sum(typical*vol)/sum(vol), typical=(h+l+c)/3."""
    typ = [(highs[i] + lows[i] + closes[i]) / 3.0 for i in range(len(closes))]
    pv = [typ[i] * volumes[i] for i in range(len(closes))]
    out = [NAN] * len(closes)
    run_pv = 0.0
    run_v = 0.0
    for i in range(len(closes)):
        run_pv += pv[i]
        run_v += volumes[i]
        if i >= n:
            run_pv -= pv[i - n]
            run_v -= volumes[i - n]
        if i >= n - 1 and run_v > 0:
            out[i] = run_pv / run_v
    return out


def top_percent_change(opens: Sequence[float], closes: Sequence[float], length: int) -> list[float]:
    """(rolling-max open over `length` - close) / close. length==0 → (open-close)/close."""
    out = [NAN] * len(closes)
    for i in range(len(closes)):
        if not closes[i]:
            continue
        if length == 0:
            out[i] = (opens[i] - closes[i]) / closes[i]
        elif i >= length - 1:
            out[i] = (max(opens[i - length + 1 : i + 1]) - closes[i]) / closes[i]
    return out


# --------------------------------------------------------------------------- #
# Heikin-Ashi
# --------------------------------------------------------------------------- #
def heikin_ashi(
    opens: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
):
    """Return (ha_open, ha_high, ha_low, ha_close), matching qtpylib.heikinashi."""
    m = len(closes)
    ha_close = [(opens[i] + highs[i] + lows[i] + closes[i]) / 4.0 for i in range(m)]
    ha_open = [NAN] * m
    ha_high = [NAN] * m
    ha_low = [NAN] * m
    if m == 0:
        return ha_open, ha_high, ha_low, ha_close
    ha_open[0] = (opens[0] + closes[0]) / 2.0
    for i in range(1, m):
        ha_open[i] = (ha_open[i - 1] + ha_close[i - 1]) / 2.0
    for i in range(m):
        ha_high[i] = max(highs[i], ha_open[i], ha_close[i])
        ha_low[i] = min(lows[i], ha_open[i], ha_close[i])
    return ha_open, ha_high, ha_low, ha_close


# --------------------------------------------------------------------------- #
# Ichimoku (technical.indicators.ichimoku semantics, forward-displaced spans)
# --------------------------------------------------------------------------- #
def _midpoint_window(highs: Sequence[float], lows: Sequence[float], n: int) -> list[float]:
    """(rolling-max high + rolling-min low) / 2, O(len) via monotonic deques."""
    from collections import deque

    out = [NAN] * len(highs)
    max_dq: deque[int] = deque()  # indices, highs decreasing
    min_dq: deque[int] = deque()  # indices, lows increasing
    for i in range(len(highs)):
        while max_dq and highs[max_dq[-1]] <= highs[i]:
            max_dq.pop()
        max_dq.append(i)
        while min_dq and lows[min_dq[-1]] >= lows[i]:
            min_dq.pop()
        min_dq.append(i)
        lo = i - n + 1
        if max_dq[0] < lo:
            max_dq.popleft()
        if min_dq[0] < lo:
            min_dq.popleft()
        if i >= n - 1:
            out[i] = (highs[max_dq[0]] + lows[min_dq[0]]) / 2.0
    return out


def ichimoku(
    highs: Sequence[float],
    lows: Sequence[float],
    conversion: int = 20,
    base: int = 60,
    lagging: int = 120,
    displacement: int = 30,
):
    """Return (tenkan, kijun, senkou_a, senkou_b).

    senkou_a/b are forward-displaced by ``displacement`` (span at index i is derived from
    data at i-displacement) — exactly the non-leading ``senkou_span_*`` columns ichiV1
    gates on, so ``close > senkou_a`` is causal (no lookahead).
    """
    tenkan = _midpoint_window(highs, lows, conversion)
    kijun = _midpoint_window(highs, lows, base)
    span_b_base = _midpoint_window(highs, lows, lagging)
    m = len(highs)
    senkou_a = [NAN] * m
    senkou_b = [NAN] * m
    for i in range(m):
        j = i - displacement
        if j >= 0 and _valid(tenkan[j]) and _valid(kijun[j]):
            senkou_a[i] = (tenkan[j] + kijun[j]) / 2.0
        if j >= 0 and _valid(span_b_base[j]):
            senkou_b[i] = span_b_base[j]
    return tenkan, kijun, senkou_a, senkou_b


# --------------------------------------------------------------------------- #
# Crossovers + higher-timeframe informative merge
# --------------------------------------------------------------------------- #
def crossed_below(a: Sequence[float], b: Sequence[float]) -> list[bool]:
    out = [False] * len(a)
    for i in range(1, len(a)):
        if _valid(a[i]) and _valid(b[i]) and _valid(a[i - 1]) and _valid(b[i - 1]):
            out[i] = a[i - 1] >= b[i - 1] and a[i] < b[i]
    return out


def merge_informative(
    base_ts: Sequence[int],
    info_close_ts: Sequence[int],
    info_values: Sequence[float],
) -> list[float]:
    """For each base bar, take the value of the last FULLY-CLOSED higher-tf bar as of it.

    Lookahead-safe (matches freqtrade ``merge_informative_pair``): a base bar at open_time
    ``t`` sees only higher-tf bars whose close_time <= t. ``info_*`` must be sorted by ts.
    """
    out = [NAN] * len(base_ts)
    j = -1
    n_info = len(info_close_ts)
    for k, t in enumerate(base_ts):
        while j + 1 < n_info and info_close_ts[j + 1] <= t:
            j += 1
        out[k] = info_values[j] if j >= 0 else NAN
    return out
