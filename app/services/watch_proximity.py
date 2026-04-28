from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

WatchProximityBand = Literal[
    "hit",
    "within_0_5_pct",
    "within_1_pct",
    "outside",
]

_SUPPORTED_PRICE_CONDITIONS = {"price_above", "price_below"}


@dataclass(frozen=True, slots=True)
class WatchProximityResult:
    market: str
    target_kind: str
    symbol: str
    condition_type: str
    threshold: float
    current: float
    distance_abs: float
    distance_pct: float
    band: WatchProximityBand
    triggered: bool
    dedupe_key: str


def _normalize_threshold_for_key(threshold: float) -> str:
    canonical = format(float(threshold), ".15g")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return canonical or "0"


def _classify_band(*, triggered: bool, distance_pct: float) -> WatchProximityBand:
    if triggered:
        return "hit"
    if distance_pct <= 0.5:
        return "within_0_5_pct"
    if distance_pct <= 1.0:
        return "within_1_pct"
    return "outside"


def build_proximity_dedupe_key(
    result: WatchProximityResult,
    band: WatchProximityBand,
) -> str:
    threshold_key = _normalize_threshold_for_key(result.threshold)
    return (
        "watch-proximity:"
        f"{result.market}:{result.target_kind}:{result.symbol}:"
        f"{result.condition_type}:{threshold_key}:{band}"
    )


def compute_price_proximity(
    *,
    market: str,
    target_kind: str,
    symbol: str,
    condition_type: str,
    threshold: float,
    current: float,
) -> WatchProximityResult:
    normalized_market = str(market or "").strip().lower()
    normalized_target_kind = str(target_kind or "asset").strip().lower()
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_condition = str(condition_type or "").strip().lower()
    threshold_value = float(threshold)
    current_value = float(current)

    if normalized_condition not in _SUPPORTED_PRICE_CONDITIONS:
        raise ValueError("condition_type must be price_above or price_below")
    if threshold_value <= 0:
        raise ValueError("threshold must be greater than zero")

    if normalized_condition == "price_above":
        raw_distance = threshold_value - current_value
        triggered = current_value >= threshold_value
    else:
        raw_distance = current_value - threshold_value
        triggered = current_value <= threshold_value

    distance_abs = raw_distance
    distance_pct = 0.0 if triggered else abs(raw_distance) / threshold_value * 100.0
    band = _classify_band(triggered=triggered, distance_pct=distance_pct)

    result_without_key = WatchProximityResult(
        market=normalized_market,
        target_kind=normalized_target_kind,
        symbol=normalized_symbol,
        condition_type=normalized_condition,
        threshold=threshold_value,
        current=current_value,
        distance_abs=distance_abs,
        distance_pct=distance_pct,
        band=band,
        triggered=triggered,
        dedupe_key="",
    )
    return WatchProximityResult(
        market=result_without_key.market,
        target_kind=result_without_key.target_kind,
        symbol=result_without_key.symbol,
        condition_type=result_without_key.condition_type,
        threshold=result_without_key.threshold,
        current=result_without_key.current,
        distance_abs=result_without_key.distance_abs,
        distance_pct=result_without_key.distance_pct,
        band=result_without_key.band,
        triggered=result_without_key.triggered,
        dedupe_key=build_proximity_dedupe_key(result_without_key, band),
    )


def format_proximity_message(results: list[WatchProximityResult]) -> str:
    if not results:
        return (
            "Watch proximity alerts\n"
            "This is an informational alert only; any order requires final user approval."
        )

    market = results[0].market
    lines = [f"Watch proximity alerts ({market})"]
    for result in results:
        lines.append(
            f"- {result.symbol} {result.condition_type}: "
            f"current={result.current:.4f}, "
            f"threshold={result.threshold:.4f}, "
            f"distance_abs={result.distance_abs:.4f}, "
            f"distance_pct={result.distance_pct:.4f}%, "
            f"band={result.band}"
        )
    lines.append(
        "This is an informational alert only; any order requires final user approval."
    )
    return "\n".join(lines)


__all__ = [
    "WatchProximityBand",
    "WatchProximityResult",
    "build_proximity_dedupe_key",
    "compute_price_proximity",
    "format_proximity_message",
]
