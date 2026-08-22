"""Point-in-time indicator reconstruction for the screener bakeoff.

These are standalone mirrors of the production deterministic level math in
``app.mcp_server.tooling.market_data_indicators`` (``_calculate_rsi``,
``_calculate_bollinger``, ``_calculate_fibonacci``, ``_format_fibonacci_source``,
``_cluster_price_levels``, ``_split_support_resistance_levels``) and of
``analysis_quick._build_support_resistance``.

They are re-implemented rather than imported so this research package can run
without booting application ``Settings``.  ``tests/research/`` carries an
equivalence test that pins these against the production functions.

LOOK-AHEAD CONTRACT: every function here consumes only the arrays it is given.
The caller is responsible for slicing bars to ``time <= decision_date``; the
loader in ``panel.py`` does that once and the slice is asserted in tests.
"""

from __future__ import annotations

import numpy as np

from research.screener_bakeoff.spec import (
    BOLLINGER_PERIOD,
    BOLLINGER_STD,
    CLUSTER_TOLERANCE_PCT,
    FIBONACCI_LEVELS,
    MODERATE_SOURCE_COUNT,
    RSI_PERIOD,
    STRONG_SOURCE_COUNT,
)


def _wilder_avgs(close: np.ndarray, period: int):
    """Reproduce pandas ``ewm(alpha=1/period, adjust=False)`` on gain/loss.

    ``close.diff()`` leaves NaN at index 0 and ``.where(delta > 0, 0.0)``
    turns that NaN into 0.0, so the production gain/loss series is
    ``[0.0, d1, d2, ...]`` — the same length as ``close`` and seeded at 0.0.
    Dropping that leading zero changes the seed and the answer.
    """
    n = close.size
    gain = np.zeros(n)
    loss = np.zeros(n)
    delta = np.diff(close)
    gain[1:] = np.where(delta > 0, delta, 0.0)
    loss[1:] = np.where(delta < 0, -delta, 0.0)
    alpha = 1.0 / period
    ag = np.empty(n)
    al = np.empty(n)
    ag[0] = gain[0]
    al[0] = loss[0]
    for i in range(1, n):
        ag[i] = ag[i - 1] + alpha * (gain[i] - ag[i - 1])
        al[i] = al[i - 1] + alpha * (loss[i] - al[i - 1])
    return ag, al


def rsi_wilder(close: np.ndarray, period: int = RSI_PERIOD) -> float | None:
    if close.size < period + 1:
        return None
    ag, al = _wilder_avgs(close, period)
    if al[-1] == 0:
        return None
    rs = ag[-1] / al[-1]
    return round(float(100.0 - (100.0 / (1.0 + rs))), 2)


def rsi_wilder_series(close: np.ndarray, period: int = RSI_PERIOD) -> np.ndarray:
    """Wilder RSI at every index (NaN before ``min_periods``, as pandas does)."""
    n = close.size
    out = np.full(n, np.nan)
    if n < period + 1:
        return out
    ag, al = _wilder_avgs(close, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.where(al > 0, ag / np.where(al > 0, al, 1.0), np.nan)
        rsi = 100.0 - (100.0 / (1.0 + rs))
    out[period - 1 :] = np.round(rsi[period - 1 :], 2)
    return out


def bollinger(close: np.ndarray) -> tuple[float | None, float | None, float | None]:
    if close.size < BOLLINGER_PERIOD:
        return None, None, None
    window = close[-BOLLINGER_PERIOD:]
    mid = float(window.mean())
    sd = float(window.std(ddof=1))
    return mid + BOLLINGER_STD * sd, mid, mid - BOLLINGER_STD * sd


def _format_fibonacci_source(level: float) -> str:
    pct = level * 100
    if abs(pct - round(pct)) < 1e-9:
        pct_str = str(int(round(pct)))
    else:
        pct_str = f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"fib_{pct_str}"


def fibonacci_levels(high: np.ndarray, low: np.ndarray) -> dict[float, float]:
    swing_high = round(float(high.max()), 2)
    swing_low = round(float(low.min()), 2)
    hi_pos = int(high.argmax())
    lo_pos = int(low.argmin())
    span = swing_high - swing_low
    if hi_pos > lo_pos:
        return {lvl: round(swing_high - lvl * span, 2) for lvl in FIBONACCI_LEVELS}
    return {lvl: round(swing_low + lvl * span, 2) for lvl in FIBONACCI_LEVELS}


def cluster_levels(levels: list[tuple[float, str]]) -> list[dict]:
    clusters: list[dict] = []
    for price, source in sorted(levels, key=lambda item: item[0]):
        if price <= 0:
            continue
        matched = None
        for cluster in clusters:
            center = cluster["center"]
            if center > 0 and abs(price - center) / center <= CLUSTER_TOLERANCE_PCT:
                matched = cluster
                break
        if matched is None:
            clusters.append({"prices": [price], "sources": [source], "center": price})
            continue
        matched["prices"].append(price)
        if source not in matched["sources"]:
            matched["sources"].append(source)
        matched["center"] = sum(matched["prices"]) / len(matched["prices"])

    out: list[dict] = []
    for cluster in clusters:
        sources = cluster["sources"]
        count = len(sources)
        if count >= STRONG_SOURCE_COUNT:
            strength = "strong"
        elif count == MODERATE_SOURCE_COUNT:
            strength = "moderate"
        else:
            strength = "weak"
        out.append(
            {
                "price": round(sum(cluster["prices"]) / len(cluster["prices"]), 2),
                "strength": strength,
                "sources": sources,
            }
        )
    return out


_FAMILY_ALIASES: tuple[tuple[str, str], ...] = (
    ("fib_", "fib"),
    ("bb_lower", "bb_lower"),
    ("volume_", "volume_profile"),
)


def source_families(sources: list[str]) -> set[str]:
    families: set[str] = set()
    for src in sources:
        for prefix, family in _FAMILY_ALIASES:
            if src.startswith(prefix):
                families.add(family)
    return families


def support_resistance(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    volume: np.ndarray,
    current_price: float,
) -> tuple[list[dict], list[dict]]:
    """Mirror of analysis_quick._build_support_resistance."""
    if current_price <= 0 or close.size == 0:
        return [], []
    levels: list[tuple[float, str]] = [
        (price, _format_fibonacci_source(lvl))
        for lvl, price in fibonacci_levels(high, low).items()
        if price > 0
    ]
    vol_sum = float(volume.sum())
    if vol_sum:
        poc = float((close * volume).sum() / vol_sum)
        if poc > 0:
            levels.append((poc, "volume_poc"))
    upper, middle, lower = bollinger(close)
    for value, name in (
        (upper, "bb_upper"),
        (middle, "bb_middle"),
        (lower, "bb_lower"),
    ):
        if value is not None and value > 0:
            levels.append((float(value), name))

    supports: list[dict] = []
    resistances: list[dict] = []
    for level in cluster_levels(levels):
        price = level["price"]
        if price <= 0:
            continue
        level["distance_pct"] = round((price - current_price) / current_price * 100, 2)
        if price < current_price:
            supports.append(level)
        elif price > current_price:
            resistances.append(level)
    supports.sort(key=lambda item: item["price"], reverse=True)
    resistances.sort(key=lambda item: item["price"])
    return supports, resistances
