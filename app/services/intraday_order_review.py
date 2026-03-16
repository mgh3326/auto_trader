"""Intraday order review classification logic."""

from __future__ import annotations

from typing import Any


def classify_fill_proximity(
    gap_pct: float | None,
    thresholds: dict[str, float] | None = None,
) -> str:
    """Classify order fill proximity based on gap percentage.

    Args:
        gap_pct: Gap between current price and order price in percent
        thresholds: Optional custom thresholds with keys: near, moderate, far

    Returns:
        Classification: "near", "moderate", "far", or "very_far"
    """
    if gap_pct is None:
        return "unknown"

    defaults = {"near": 2.0, "moderate": 5.0, "far": 10.0}
    t = {**defaults, **(thresholds or {})}

    abs_gap = abs(gap_pct)
    if abs_gap <= t["near"]:
        return "near"
    elif abs_gap <= t["moderate"]:
        return "moderate"
    elif abs_gap <= t["far"]:
        return "far"
    else:
        return "very_far"


def format_fill_proximity(proximity: str, gap_pct: float | None = None) -> str:
    """Format fill proximity for display."""
    labels = {
        "near": "체결 임박 ⚡",
        "moderate": "체결 근접",
        "far": "체결 거리",
        "very_far": "체결 멈",
        "unknown": "알 수 없음",
    }
    return labels.get(proximity, proximity)


def check_needs_attention(
    order: dict[str, Any],
    indicators: dict[str, Any] | None,
    thresholds: dict[str, Any] | None = None,
) -> tuple[bool, str | None]:
    """Check if an order needs attention based on market conditions.

    Args:
        order: Normalized order dict with gap_pct, side fields
        indicators: Market indicators dict with rsi_14, change_24h_pct
        thresholds: Optional custom thresholds

    Returns:
        Tuple of (needs_attention, reason_string)
    """
    defaults = {
        "near_fill_pct": 2.0,
        "market_volatility_pct": 5.0,
        "rsi_overbought": 70,
        "rsi_oversold": 30,
        "far_order_pct": 15.0,
    }
    t = {**defaults, **(thresholds or {})}

    reasons = []
    gap_pct = order.get("gap_pct")
    side = order.get("side", "").lower()
    rsi = indicators.get("rsi_14") if indicators else None
    change_24h = indicators.get("change_24h_pct", 0) if indicators else 0

    # Near fill (any side)
    if gap_pct is not None and abs(gap_pct) <= t["near_fill_pct"]:
        reasons.append(f"체결 임박 ({gap_pct:+.1f}%)")

    # Market volatility
    if abs(change_24h) >= t["market_volatility_pct"]:
        reasons.append(f"24h {change_24h:+.1f}% 급변")

    # RSI extremes - different for buy vs sell
    if rsi is not None:
        if side == "buy" and rsi >= t["rsi_overbought"]:
            reasons.append(f"RSI {rsi:.0f} 과매수 (매수 재검토)")
        if side == "sell" and rsi <= t["rsi_oversold"]:
            reasons.append(f"RSI {rsi:.0f} 과매도 (매도 재검토)")

    # Very far order (capital locked)
    if gap_pct is not None and abs(gap_pct) >= t["far_order_pct"]:
        reasons.append(f"현재가 대비 {abs(gap_pct):.0f}% 이탈 (자금 묶임)")

    if reasons:
        return True, " / ".join(reasons)
    return False, None


__all__ = [
    "classify_fill_proximity",
    "format_fill_proximity",
    "check_needs_attention",
]
