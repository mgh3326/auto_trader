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


VOLUME_PROFILE_BINS = 20
VOLUME_PROFILE_VALUE_AREA_RATIO = 0.70


def _normalize_number(value: float, decimals: int = 6) -> float | int:
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return int(round(rounded))
    return rounded


def volume_profile_levels(
    high: np.ndarray,
    low: np.ndarray,
    volume: np.ndarray,
    bins: int = VOLUME_PROFILE_BINS,
    value_area_ratio: float = VOLUME_PROFILE_VALUE_AREA_RATIO,
) -> tuple[float, float, float] | None:
    """Mirror of market_data_indicators._calculate_volume_profile.

    Returns ``(poc, value_area_high, value_area_low)`` or ``None`` where the
    production helper raises (no valid rows / zero total volume).
    """
    valid = ~(np.isnan(high) | np.isnan(low) | np.isnan(volume))
    if not valid.any():
        return None
    candle_low = np.minimum(low[valid], high[valid]).astype(float)
    candle_high = np.maximum(low[valid], high[valid]).astype(float)
    candle_volume = volume[valid].astype(float)
    price_low = float(candle_low.min())
    price_high = float(candle_high.max())
    if price_high <= price_low:
        epsilon = max(abs(price_low) * 1e-6, 1e-6)
        edges = np.linspace(price_low - epsilon / 2, price_high + epsilon / 2, bins + 1)
    else:
        edges = np.linspace(price_low, price_high, bins + 1)

    bin_volumes = np.zeros(bins, dtype=float)
    for low_i, high_i, vol_i in zip(
        candle_low, candle_high, candle_volume, strict=False
    ):
        if vol_i <= 0:
            continue
        if high_i <= low_i:
            idx = int(
                np.clip(np.searchsorted(edges, low_i, side="right") - 1, 0, bins - 1)
            )
            bin_volumes[idx] += vol_i
            continue
        overlaps = np.clip(
            np.minimum(edges[1:], high_i) - np.maximum(edges[:-1], low_i), 0.0, None
        )
        overlap_sum = float(overlaps.sum())
        if overlap_sum <= 0:
            mid = (low_i + high_i) / 2
            idx = int(
                np.clip(np.searchsorted(edges, mid, side="right") - 1, 0, bins - 1)
            )
            bin_volumes[idx] += vol_i
            continue
        bin_volumes += vol_i * (overlaps / overlap_sum)

    total = float(bin_volumes.sum())
    if total <= 0:
        return None
    poc_index = int(np.argmax(bin_volumes))
    target = total * value_area_ratio
    covered = float(bin_volumes[poc_index])
    left = right = poc_index
    while covered < target and (left > 0 or right < bins - 1):
        left_vol = bin_volumes[left - 1] if left > 0 else -np.inf
        right_vol = bin_volumes[right + 1] if right < bins - 1 else -np.inf
        if right_vol > left_vol:
            right += 1
            covered += float(bin_volumes[right])
        elif left > 0:
            left -= 1
            covered += float(bin_volumes[left])
        elif right < bins - 1:
            right += 1
            covered += float(bin_volumes[right])
        else:
            break
    return (
        float(_normalize_number((edges[poc_index] + edges[poc_index + 1]) / 2)),
        float(_normalize_number(edges[right + 1])),
        float(_normalize_number(edges[left])),
    )


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
    profile = volume_profile_levels(high, low, volume)
    if profile is not None:
        for value, name in zip(
            profile,
            ("volume_poc", "volume_value_area_high", "volume_value_area_low"),
            strict=True,
        ):
            if value > 0:
                levels.append((value, name))
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
