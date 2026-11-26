"""Pure technical indicator and support/resistance math.

The single implementation of Wilder RSI-14 and of the support/resistance levels
(ROB-1382). It lives outside ``app.mcp_server`` so database-only services can
use it without importing a broker client;
``app.mcp_server.tooling.market_data_indicators`` re-exports every function
under its previous private name.
"""

from __future__ import annotations

import datetime
from typing import Any

import numpy as np
import pandas as pd

from app.core.price_rounding import (
    round_price_for_market,
    to_float,
    to_optional_float,
)

DEFAULT_RSI_PERIOD = 14
DEFAULT_BOLLINGER_PERIOD = 20
DEFAULT_BOLLINGER_STD = 2.0

FIBONACCI_LEVELS = [0.0, 0.236, 0.382, 0.5, 0.618, 0.786, 1.0]


def calculate_rsi_series(
    close: pd.Series, period: int = DEFAULT_RSI_PERIOD
) -> pd.Series:
    """Wilder-smoothed RSI series (ewm alpha=1/period over all ``period``
    deltas, ``adjust=False``).

    ROB-1382: this is the single RSI implementation for every MCP surface.
    ``analyze_stock_batch(quick)`` reaches it via ``_compute_indicators`` →
    ``calculate_rsi``; ``get_ohlcv(include_indicators=True)`` enriches rows
    through ``market_data_quotes._enrich_ohlcv_with_indicators``, which calls
    this same function.
    """
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta).where(delta < 0, 0.0)

    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()

    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def calculate_rsi(
    close: pd.Series, period: int = DEFAULT_RSI_PERIOD
) -> dict[str, float | None]:
    if len(close) < period + 1:
        return {str(period): None}

    rsi = calculate_rsi_series(close, period)
    rsi_value = rsi.iloc[-1]
    return {str(period): round(float(rsi_value), 2) if pd.notna(rsi_value) else None}


def calculate_bollinger(
    close: pd.Series,
    period: int = DEFAULT_BOLLINGER_PERIOD,
    std: float = DEFAULT_BOLLINGER_STD,
) -> dict[str, float | None]:
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


def calculate_fibonacci(
    df: pd.DataFrame, current_price: float, market_type: str | None = None
) -> dict[str, Any]:
    high = df["high"].astype(float)
    low = df["low"].astype(float)

    swing_high_price = round_price_for_market(float(high.max()), market_type)
    swing_low_price = round_price_for_market(float(low.min()), market_type)
    swing_high_pos = int(high.values.argmax())
    swing_low_pos = int(low.values.argmin())

    def _to_date_str(row: pd.Series) -> str:
        d = row.get("date")
        if d is None:
            return ""
        if isinstance(d, str):
            return d[:10]
        if isinstance(d, (datetime.date, datetime.datetime, pd.Timestamp)):
            return d.strftime("%Y-%m-%d")
        return str(d)[:10]

    swing_high_date = _to_date_str(df.iloc[swing_high_pos])
    swing_low_date = _to_date_str(df.iloc[swing_low_pos])

    if swing_high_pos > swing_low_pos:
        trend = "retracement_from_high"
        levels = {
            str(lvl): round_price_for_market(
                swing_high_price - lvl * (swing_high_price - swing_low_price),
                market_type,
            )
            for lvl in FIBONACCI_LEVELS
        }
    else:
        trend = "bounce_from_low"
        levels = {
            str(lvl): round_price_for_market(
                swing_low_price + lvl * (swing_high_price - swing_low_price),
                market_type,
            )
            for lvl in FIBONACCI_LEVELS
        }

    nearest_support: dict[str, Any] | None = None
    nearest_resistance: dict[str, Any] | None = None
    sorted_levels = sorted(levels.items(), key=lambda x: x[1])
    for level_str, price in sorted_levels:
        if price < current_price:
            nearest_support = {"level": level_str, "price": price}
        elif price > current_price and nearest_resistance is None:
            nearest_resistance = {"level": level_str, "price": price}

    return {
        "swing_high": {"price": swing_high_price, "date": swing_high_date},
        "swing_low": {"price": swing_low_price, "date": swing_low_date},
        "trend": trend,
        "current_price": current_price,
        "levels": levels,
        "nearest_support": nearest_support,
        "nearest_resistance": nearest_resistance,
    }


def format_fibonacci_source(level_key: str) -> str:
    level = to_optional_float(level_key)
    if level is None:
        return f"fib_{level_key}"

    pct = level * 100
    if abs(pct - round(pct)) < 1e-9:
        pct_str = str(int(round(pct)))
    else:
        pct_str = f"{pct:.1f}".rstrip("0").rstrip(".")
    return f"fib_{pct_str}"


def cluster_price_levels(
    levels: list[tuple[float, str]],
    tolerance_pct: float = 0.02,
    market_type: str | None = None,
) -> list[dict[str, Any]]:
    if not levels:
        return []

    clusters: list[dict[str, Any]] = []
    for price, source in sorted(levels, key=lambda item: item[0]):
        if price <= 0:
            continue

        matched_cluster: dict[str, Any] | None = None
        for cluster in clusters:
            center = to_float(cluster.get("center"), default=0.0)
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
                "price": round_price_for_market(sum(prices) / len(prices), market_type),
                "strength": strength,
                "sources": level_sources,
            }
        )

    return clustered


def split_support_resistance_levels(
    clustered_levels: list[dict[str, Any]],
    current_price: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    supports: list[dict[str, Any]] = []
    resistances: list[dict[str, Any]] = []

    for level in clustered_levels:
        price = to_float(level.get("price"), default=0.0)
        if price <= 0:
            continue
        level["distance_pct"] = round((price - current_price) / current_price * 100, 2)
        if price < current_price:
            supports.append(level)
        elif price > current_price:
            resistances.append(level)

    supports.sort(
        key=lambda item: to_float(item.get("price"), default=0.0), reverse=True
    )
    resistances.sort(key=lambda item: to_float(item.get("price"), default=0.0))
    return supports, resistances


def collect_support_resistance_price_levels(
    fibonacci: dict[str, Any] | None,
    volume_profile: dict[str, Any] | None,
    indicators: dict[str, Any] | None,
) -> list[tuple[float, str]]:
    """Single source of the (price, source) inputs for support/resistance.

    ``get_support_resistance`` and ``analyze_stock_batch(quick=True)`` both
    cluster these levels; building them here keeps ``volume_poc`` (the binned
    volume-profile POC, not a volume-weighted close), the value-area bounds and
    the Bollinger levels identical across the two tools.
    """
    levels: list[tuple[float, str]] = []

    fib_levels = (fibonacci or {}).get("levels")
    if isinstance(fib_levels, dict):
        for level_key, price in fib_levels.items():
            level_price = to_optional_float(price)
            if level_price is not None and level_price > 0:
                levels.append((level_price, format_fibonacci_source(str(level_key))))

    profile = volume_profile or {}
    poc_price = to_optional_float((profile.get("poc") or {}).get("price"))
    if poc_price is not None and poc_price > 0:
        levels.append((poc_price, "volume_poc"))
    value_area = profile.get("value_area") or {}
    for key, source in (
        ("high", "volume_value_area_high"),
        ("low", "volume_value_area_low"),
    ):
        level_price = to_optional_float(value_area.get(key))
        if level_price is not None and level_price > 0:
            levels.append((level_price, source))

    indicator_result = indicators or {}
    bollinger = indicator_result.get("bollinger")
    if not isinstance(bollinger, dict):
        nested = indicator_result.get("indicators")
        bollinger = nested.get("bollinger") if isinstance(nested, dict) else None
    if not isinstance(bollinger, dict):
        bollinger = {}
    for key, source in (
        ("upper", "bb_upper"),
        ("middle", "bb_middle"),
        ("lower", "bb_lower"),
    ):
        level_price = to_optional_float(bollinger.get(key))
        if level_price is not None and level_price > 0:
            levels.append((level_price, source))

    return levels


def normalize_number(value: float, decimals: int = 6) -> float | int:
    rounded = round(float(value), decimals)
    if abs(rounded - round(rounded)) < 10 ** (-decimals):
        return int(round(rounded))
    return rounded


def calculate_volume_profile(
    df: pd.DataFrame,
    bins: int,
    value_area_ratio: float = 0.70,
) -> dict[str, Any]:
    if bins < 2:
        raise ValueError("bins must be >= 2")
    if not 0 < value_area_ratio <= 1:
        raise ValueError("value_area_ratio must be between 0 and 1")

    required = {"low", "high", "volume"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required columns: {missing}")
    if df.empty:
        raise ValueError("No OHLCV data available")

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
            price_low - epsilon / 2,
            price_high + epsilon / 2,
            bins + 1,
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
                    np.searchsorted(bin_edges, mid_price, side="right") - 1,
                    0,
                    bins - 1,
                )
            )
            bin_volumes[idx] += vol_i
            continue

        bin_volumes += vol_i * (overlaps / overlap_sum)

    total_volume = float(bin_volumes.sum())
    if total_volume <= 0:
        raise ValueError("Total volume is zero for the selected period")

    bin_volume_pct = (bin_volumes / total_volume) * 100
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

    profile = [
        {
            "price_low": normalize_number(bin_edges[idx], decimals=6),
            "price_high": normalize_number(bin_edges[idx + 1], decimals=6),
            "volume": normalize_number(bin_volumes[idx], decimals=2),
            "volume_pct": normalize_number(bin_volume_pct[idx], decimals=2),
        }
        for idx in range(bins)
    ]

    return {
        "price_range": {
            "low": normalize_number(price_low, decimals=6),
            "high": normalize_number(price_high, decimals=6),
        },
        "poc": {
            "price": normalize_number(
                (bin_edges[poc_index] + bin_edges[poc_index + 1]) / 2,
                decimals=6,
            ),
            "volume": normalize_number(bin_volumes[poc_index], decimals=2),
        },
        "value_area": {
            "high": normalize_number(bin_edges[right_index + 1], decimals=6),
            "low": normalize_number(bin_edges[left_index], decimals=6),
            "volume_pct": normalize_number(
                (covered_volume / total_volume) * 100, decimals=2
            ),
        },
        "profile": profile,
    }
