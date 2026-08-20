"""Pure port of the repository's support/resistance and RSI arithmetic.

Why a port and not an import
----------------------------
``app.mcp_server.tooling.market_data_indicators`` holds the production
clustering logic, but importing it pulls ``app.core.config.settings`` in
through the broker clients, which raises unless KIS/Upbit/OpenDART
credentials and ``DATABASE_URL`` are present in the environment.  The
pre-registered spec requires this study to run with zero network, zero
database and no credentials, so the arithmetic is reproduced here instead.

The port is not an approximation.  ``tests/test_levels_match_repo.py``
imports the production functions (under throw-away dummy settings) and
asserts value-for-value equality on randomised frames.  That test is opt-in
and skips when the import fails; the study itself never imports ``app``.

One deliberate deviation: the production helpers ``round(price, 2)`` because
they feed a JSON display surface.  Two decimals silently destroys sub-KRW
crypto prices, so ``price_decimals`` defaults to ``None`` (full precision)
here.  Passing ``price_decimals=2`` reproduces production byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pandas as pd

# Mirrors app.mcp_server.tooling.market_data_indicators.
FIBONACCI_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]
DEFAULT_RSI_PERIOD = 14
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_STD = 2.0
DEFAULT_VOLUME_PROFILE_BINS = 20
DEFAULT_CLUSTER_TOLERANCE_PCT = 0.02

Strength = Literal["weak", "moderate", "strong"]


def _maybe_round(value: float, decimals: int | None) -> float:
    return float(value) if decimals is None else round(float(value), decimals)


def rsi_series(close: pd.Series, period: int = DEFAULT_RSI_PERIOD) -> pd.Series:
    """Wilder RSI, identical recursion to ``_calculate_rsi``.

    The production helper returns only the last value; a full series is needed
    here because every bar in a symbol is a candidate event day.  The EWM
    recursion is causal, so ``rsi_series(close)[i]`` uses ``close[:i + 1]``
    only — see ``tests/test_no_lookahead.py``.
    """
    close = close.astype(float)
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calculate_fibonacci(
    df: pd.DataFrame,
    current_price: float,
    *,
    price_decimals: int | None = None,
) -> dict[str, Any]:
    """Port of ``_calculate_fibonacci`` (date strings dropped as unused)."""
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    swing_high_price = _maybe_round(float(high.max()), price_decimals)
    swing_low_price = _maybe_round(float(low.min()), price_decimals)
    swing_high_pos = int(high.values.argmax())
    swing_low_pos = int(low.values.argmin())

    span = swing_high_price - swing_low_price
    if swing_high_pos > swing_low_pos:
        trend = "retracement_from_high"
        levels = {
            str(lvl): _maybe_round(swing_high_price - lvl * span, price_decimals)
            for lvl in FIBONACCI_LEVELS
        }
    else:
        trend = "bounce_from_low"
        levels = {
            str(lvl): _maybe_round(swing_low_price + lvl * span, price_decimals)
            for lvl in FIBONACCI_LEVELS
        }

    return {
        "swing_high": {"price": swing_high_price},
        "swing_low": {"price": swing_low_price},
        "trend": trend,
        "current_price": current_price,
        "levels": levels,
    }


def calculate_bollinger(
    close: pd.Series,
    period: int = DEFAULT_BOLLINGER_PERIOD,
    std: float = DEFAULT_BOLLINGER_STD,
) -> dict[str, float | None]:
    """Port of ``_calculate_bollinger`` (pandas sample stddev, ddof=1)."""
    close = close.astype(float)
    if len(close) < period:
        return {"upper": None, "middle": None, "lower": None}
    sma = close.rolling(window=period).mean()
    rolling_std = close.rolling(window=period).std()
    upper = sma + (rolling_std * std)
    lower = sma - (rolling_std * std)
    sma_val = sma.iloc[-1]
    upper_val = upper.iloc[-1]
    lower_val = lower.iloc[-1]
    return {
        "upper": float(upper_val) if pd.notna(upper_val) else None,
        "middle": float(sma_val) if pd.notna(sma_val) else None,
        "lower": float(lower_val) if pd.notna(lower_val) else None,
    }


def _normalize_number(value: float, decimals: int = 6) -> float | int:
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return int(round(rounded))
    return rounded


def calculate_volume_profile(
    df: pd.DataFrame,
    bins: int = DEFAULT_VOLUME_PROFILE_BINS,
    value_area_ratio: float = 0.70,
) -> dict[str, Any]:
    """Port of ``_calculate_volume_profile`` (POC + value area only).

    The per-bin ``profile`` list of the production helper is dropped: the
    caller only ever consumed ``poc.price`` and the two value-area edges.
    """
    if bins < 2:
        raise ValueError("bins must be >= 2")
    if not 0 < value_area_ratio <= 1:
        raise ValueError("value_area_ratio must be between 0 and 1")

    low = pd.to_numeric(df["low"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    valid_mask = low.notna() & high.notna() & volume.notna()
    if not valid_mask.any():
        raise ValueError("No valid OHLCV rows with low/high/volume")

    low_values = low[valid_mask].astype(float).to_numpy()
    high_values = high[valid_mask].astype(float).to_numpy()
    candle_low = np.minimum(low_values, high_values)
    candle_high = np.maximum(low_values, high_values)
    candle_volume = volume[valid_mask].astype(float).to_numpy()

    price_low = float(candle_low.min())
    price_high = float(candle_high.max())
    if price_high <= price_low:
        epsilon = max(abs(price_low) * 1e-6, 1e-6)
        bin_edges = np.linspace(
            price_low - epsilon / 2, price_high + epsilon / 2, bins + 1
        )
    else:
        bin_edges = np.linspace(price_low, price_high, bins + 1)

    bin_volumes = np.zeros(bins, dtype=float)
    for low_i, high_i, vol_i in zip(
        candle_low, candle_high, candle_volume, strict=False
    ):
        if vol_i <= 0:
            continue
        if high_i <= low_i:
            idx = int(
                np.clip(
                    np.searchsorted(bin_edges, low_i, side="right") - 1, 0, bins - 1
                )
            )
            bin_volumes[idx] += vol_i
            continue
        overlaps = np.minimum(bin_edges[1:], high_i) - np.maximum(bin_edges[:-1], low_i)
        overlaps = np.clip(overlaps, 0.0, None)
        overlap_sum = float(overlaps.sum())
        if overlap_sum <= 0:
            mid_price = (low_i + high_i) / 2
            idx = int(
                np.clip(
                    np.searchsorted(bin_edges, mid_price, side="right") - 1, 0, bins - 1
                )
            )
            bin_volumes[idx] += vol_i
            continue
        bin_volumes += vol_i * (overlaps / overlap_sum)

    total_volume = float(bin_volumes.sum())
    if total_volume <= 0:
        raise ValueError("Total volume is zero for the selected period")

    poc_index = int(np.argmax(bin_volumes))
    target_volume = total_volume * value_area_ratio
    covered_volume = float(bin_volumes[poc_index])
    left_index = poc_index
    right_index = poc_index
    while covered_volume < target_volume and (left_index > 0 or right_index < bins - 1):
        left_vol = bin_volumes[left_index - 1] if left_index > 0 else -np.inf
        right_vol = bin_volumes[right_index + 1] if right_index < bins - 1 else -np.inf
        if right_vol > left_vol:
            right_index += 1
            covered_volume += float(bin_volumes[right_index])
        else:
            if left_index > 0:
                left_index -= 1
                covered_volume += float(bin_volumes[left_index])
            elif right_index < bins - 1:
                right_index += 1
                covered_volume += float(bin_volumes[right_index])
            else:
                break

    return {
        "poc": {
            "price": _normalize_number(
                (bin_edges[poc_index] + bin_edges[poc_index + 1]) / 2, decimals=6
            )
        },
        "value_area": {
            "high": _normalize_number(bin_edges[right_index + 1], decimals=6),
            "low": _normalize_number(bin_edges[left_index], decimals=6),
        },
    }


def format_fibonacci_source(level_key: str) -> str:
    """Port of ``_format_fibonacci_source``."""
    try:
        level = float(level_key)
    except (TypeError, ValueError):
        return f"fib_{level_key}"
    pct = level * 100
    if abs(pct - round(pct)) < 1e-9:
        pct_str = str(int(round(pct)))
    else:
        pct_str = f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"fib_{pct_str}"


def cluster_price_levels(
    levels: list[tuple[float, str]],
    tolerance_pct: float = DEFAULT_CLUSTER_TOLERANCE_PCT,
    *,
    price_decimals: int | None = None,
) -> list[dict[str, Any]]:
    """Port of ``_cluster_price_levels``.

    Strength is source *diversity*, not hit count: >=3 distinct sources is
    ``strong``, exactly 2 is ``moderate``, 1 is ``weak``.  Option (3)'s rebid
    price is the nearest ``strong`` support, so this threshold is load-bearing.
    """
    if not levels:
        return []

    clusters: list[dict[str, Any]] = []
    for price, source in sorted(levels, key=lambda item: item[0]):
        if price <= 0:
            continue
        matched_cluster: dict[str, Any] | None = None
        for cluster in clusters:
            center = float(cluster.get("center") or 0.0)
            if center <= 0:
                continue
            if abs(price - center) / center <= tolerance_pct:
                matched_cluster = cluster
                break
        if matched_cluster is None:
            clusters.append({"prices": [price], "sources": [source], "center": price})
            continue
        prices = matched_cluster["prices"]
        sources = matched_cluster["sources"]
        prices.append(price)
        if source not in sources:
            sources.append(source)
        matched_cluster["center"] = sum(prices) / len(prices)

    clustered: list[dict[str, Any]] = []
    for cluster in clusters:
        prices = cluster.get("prices", [])
        if not prices:
            continue
        level_sources = cluster.get("sources", [])
        source_count = len(level_sources)
        if source_count >= 3:
            strength = "strong"
        elif source_count == 2:
            strength = "moderate"
        else:
            strength = "weak"
        clustered.append(
            {
                "price": _maybe_round(sum(prices) / len(prices), price_decimals),
                "strength": strength,
                "sources": level_sources,
            }
        )
    return clustered


def split_support_resistance_levels(
    clustered_levels: list[dict[str, Any]],
    current_price: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Port of ``_split_support_resistance_levels``."""
    supports: list[dict[str, Any]] = []
    resistances: list[dict[str, Any]] = []
    for level in clustered_levels:
        price = float(level.get("price") or 0.0)
        if price <= 0:
            continue
        level = dict(level)
        level["distance_pct"] = round((price - current_price) / current_price * 100, 2)
        if price < current_price:
            supports.append(level)
        elif price > current_price:
            resistances.append(level)
    supports.sort(key=lambda item: float(item["price"]), reverse=True)
    resistances.sort(key=lambda item: float(item["price"]))
    return supports, resistances


@dataclass(frozen=True)
class LevelView:
    """One bar's overhead/underfoot structure, computed from that bar back."""

    current_price: float
    supports: tuple[dict[str, Any], ...]
    resistances: tuple[dict[str, Any], ...]

    @property
    def resistance_count(self) -> int:
        return len(self.resistances)

    @property
    def named_resistance_count(self) -> int:
        """Resistances corroborated by at least two independent sources.

        A ``weak`` cluster carries exactly one source.  On a bar closing at a
        new window high that lone source is ``fib_0`` — the bar's own high —
        so counting it as overhead resistance would make "no resistance
        overhead" almost unreachable by construction.
        """
        return sum(1 for level in self.resistances if level.get("strength") != "weak")

    def nearest_support(
        self, strength: Strength | tuple[Strength, ...] | None = None
    ) -> float | None:
        """Highest support strictly below price, optionally filtered by strength.

        ``supports`` is already sorted descending by price, so the first match
        is the nearest one below.
        """
        if isinstance(strength, str):
            allowed: tuple[str, ...] | None = (strength,)
        else:
            allowed = strength
        for level in self.supports:
            if allowed is not None and level.get("strength") not in allowed:
                continue
            return float(level["price"])
        return None


def compute_levels(
    window: pd.DataFrame,
    *,
    current_price: float | None = None,
    bins: int = DEFAULT_VOLUME_PROFILE_BINS,
    tolerance_pct: float = DEFAULT_CLUSTER_TOLERANCE_PCT,
    price_decimals: int | None = None,
) -> LevelView:
    """Port of ``get_support_resistance_impl``'s pure core.

    ``window`` must be the trailing bars **ending at and including** the
    decision bar, with ``high``/``low``/``close``/``volume`` columns.  Nothing
    after the last row is read, which is what makes the study look-ahead free.
    """
    if window.empty:
        raise ValueError("window is empty")
    price = (
        float(window["close"].iloc[-1])
        if current_price is None
        else float(current_price)
    )
    if price <= 0:
        raise ValueError("current price must be positive")

    fib = calculate_fibonacci(window, price, price_decimals=price_decimals)
    volume_profile = calculate_volume_profile(window, bins=bins)
    bollinger = calculate_bollinger(window["close"])

    price_levels: list[tuple[float, str]] = []
    for level_key, level_price in fib["levels"].items():
        if level_price is not None and level_price > 0:
            price_levels.append(
                (float(level_price), format_fibonacci_source(str(level_key)))
            )

    poc_price = volume_profile["poc"]["price"]
    if poc_price is not None and poc_price > 0:
        price_levels.append((float(poc_price), "volume_poc"))
    value_area = volume_profile["value_area"]
    for key, source in (
        ("high", "volume_value_area_high"),
        ("low", "volume_value_area_low"),
    ):
        edge = value_area.get(key)
        if edge is not None and edge > 0:
            price_levels.append((float(edge), source))
    for key, source in (
        ("upper", "bb_upper"),
        ("middle", "bb_middle"),
        ("lower", "bb_lower"),
    ):
        band = bollinger.get(key)
        if band is not None and band > 0:
            price_levels.append((float(band), source))

    clustered = cluster_price_levels(
        price_levels, tolerance_pct=tolerance_pct, price_decimals=price_decimals
    )
    supports, resistances = split_support_resistance_levels(clustered, price)
    return LevelView(
        current_price=price,
        supports=tuple(supports),
        resistances=tuple(resistances),
    )
