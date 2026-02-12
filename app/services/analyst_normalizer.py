"""
Shared rating normalization and consensus building utilities for analyst data.

This module provides functions to normalize analyst rating labels to standard
English labels, classify them into aggregation buckets, and build consensus
statistics with extended fields.
"""

from typing import Any, Literal

# Rating label to standard English label mapping
RATING_LABEL_MAP: dict[str, str] = {
    # Korean ratings
    "매수": "Buy",
    "강력매수": "Strong Buy",
    "강매": "Strong Buy",
    "비중확대": "Buy",
    "매도": "Sell",
    "비중축소": "Sell",
    "중립": "Hold",
    "보유": "Hold",
    "매수유지": "Hold",
    "투자의견": "Hold",
    # English ratings - canonical forms (case-insensitive)
    "buy": "Buy",
    "strong buy": "Strong Buy",
    "trading buy": "Buy",
    "overweight": "Overweight",
    "outperform": "Buy",
    "sell": "Sell",
    "strong sell": "Sell",
    "underweight": "Underweight",
    "underperform": "Sell",
    "hold": "Hold",
    "neutral": "Hold",
    "market perform": "Hold",
    "marketperform": "Hold",
    "equal weight": "Hold",
    "equalweight": "Hold",
}


def normalize_rating_label(raw: str | None) -> str:
    """Normalize rating string to standard English label.

    Args:
        raw: Raw rating string (case-insensitive)

    Returns:
        Standard label: "Strong Buy", "Buy", "Hold", "Sell", "Overweight", or "Underweight"
        Defaults to "Hold" if None or not found in map
    """
    if not raw:
        return "Hold"

    normalized = raw.strip().lower()
    return RATING_LABEL_MAP.get(normalized, "Hold")


def rating_to_bucket(label: str) -> Literal["buy", "hold", "sell"]:
    """Convert standard rating label to aggregation bucket.

    Args:
        label: Standard rating label (e.g., "Strong Buy", "Buy", "Hold", etc.)

    Returns:
        Aggregation bucket: "buy", "hold", or "sell"
        Defaults to "hold" for unknown labels
    """
    if not label:
        return "hold"

    label_lower = label.strip().lower()

    if "strong" in label_lower and "buy" in label_lower:
        return "buy"
    if "overweight" in label_lower:
        return "buy"
    if "buy" in label_lower:
        return "buy"
    if "outperform" in label_lower:
        return "buy"
    if "sell" in label_lower:
        return "sell"
    if "underweight" in label_lower:
        return "sell"
    if "underperform" in label_lower:
        return "sell"

    return "hold"


def is_strong_buy(label: str) -> bool:
    """Check if rating label indicates a strong buy recommendation.

    Args:
        label: Standard rating label

    Returns:
        True if label indicates strong buy, False otherwise
    """
    if not label:
        return False
    label_lower = label.strip().lower()
    return "strong" in label_lower and "buy" in label_lower


def build_consensus(
    opinions: list[dict[str, Any]],
    current_price: int | float | None,
) -> dict[str, Any]:
    """Build consensus statistics from analyst opinions.

    Args:
        opinions: List of individual opinions with rating_bucket and target_price
        current_price: Current stock price

    Returns:
        Dictionary with consensus statistics including:
        - buy_count, hold_count, sell_count: Counts by bucket
        - strong_buy_count: Count of strong buy recommendations
        - count: Alias for total_count
        - avg_target_price, median_target_price, min_target_price, max_target_price
        - upside_pct: Upside percentage from current price
        - upside_potential: Alias for upside_pct
        - current_price: Current stock price
    """
    rating_counts: dict[str, int] = {"buy": 0, "hold": 0, "sell": 0}
    strong_buy_count = 0

    for op in opinions:
        rating_label = op.get("rating", op.get("rating_label", ""))
        normalized_label = normalize_rating_label(rating_label)
        rating_bucket = op.get("rating_bucket") or rating_to_bucket(normalized_label)

        if rating_bucket in rating_counts:
            rating_counts[rating_bucket] += 1

        if is_strong_buy(normalized_label):
            strong_buy_count += 1

    target_prices = [
        op["target_price"]
        for op in opinions
        if isinstance(op.get("target_price"), (int, float)) and op["target_price"] > 0
    ]

    consensus: dict[str, Any] = {
        "buy_count": rating_counts["buy"],
        "hold_count": rating_counts["hold"],
        "sell_count": rating_counts["sell"],
        "strong_buy_count": strong_buy_count,
        "total_count": len(opinions),
        "count": len(opinions),
        "avg_target_price": None,
        "median_target_price": None,
        "min_target_price": None,
        "max_target_price": None,
        "upside_pct": None,
        "upside_potential": None,
        "current_price": current_price,
    }

    if target_prices:
        consensus["avg_target_price"] = int(sum(target_prices) / len(target_prices))
        sorted_prices = sorted(target_prices)
        n = len(sorted_prices)
        if n % 2 == 0:
            consensus["median_target_price"] = int(
                (sorted_prices[n // 2 - 1] + sorted_prices[n // 2]) / 2
            )
        else:
            consensus["median_target_price"] = int(sorted_prices[n // 2])
        consensus["min_target_price"] = int(min(target_prices))
        consensus["max_target_price"] = int(max(target_prices))

        if current_price and isinstance(current_price, (int, float)):
            consensus["upside_pct"] = round(
                (consensus["avg_target_price"] - current_price) / current_price * 100, 2
            )
            consensus["upside_potential"] = consensus["upside_pct"]

    return consensus
