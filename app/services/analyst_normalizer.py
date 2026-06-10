"""
Shared rating normalization and consensus building utilities for analyst data.

This module provides functions to normalize analyst rating labels to standard
English labels, classify them into aggregation buckets, and build consensus
statistics with extended fields.
"""

import calendar
from datetime import UTC, date, datetime
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


def _months_before(anchor: date, months: int) -> date:
    """anchor 에서 months 개월 전 날짜 (말일 클램프, 외부 의존성 없음)."""
    total = anchor.year * 12 + (anchor.month - 1) - months
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(anchor.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def _parse_opinion_date(value: Any) -> date | None:
    """행별 ISO date(YYYY-MM-DD[…]) 파싱. 부재/파싱불가 → None (fail-closed)."""
    if not isinstance(value, str):
        return None
    raw = value.strip()[:10]
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def build_consensus(
    opinions: list[dict[str, Any]],
    current_price: int | float | None,
    *,
    window_months: int = 12,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build consensus statistics from analyst opinions within a recency window.

    ROB-486: 목표가 통계와 buy/hold/sell 카운트는 **window_months 이내 date 가
    있는 행(생존 집합)에서만** 집계한다. date 가 없거나 파싱 불가한 행은
    제외하고 메타데이터로만 카운트한다 (fail-closed — 조용한 혼입 금지).
    생존 행이 0이면 목표가 통계와 upside_pct 는 전부 None 이며 무필터 평균으로
    폴백하지 않는다.

    Args:
        opinions: List of individual opinions with rating_bucket, target_price,
            and per-row ISO ``date`` (YYYY-MM-DD)
        current_price: Current stock price
        window_months: Recency window in months (default 12)
        now: 집계 기준 시각 (테스트 주입용; 기본 현재 UTC)

    Returns:
        Dictionary with consensus statistics including:
        - buy_count, hold_count, sell_count, strong_buy_count: windowed counts
        - total_count: 생존(windowed) 행 수 (== rows_used)
        - avg_target_price, median_target_price, min_target_price, max_target_price
        - upside_pct: Upside percentage from current price (windowed avg 기준)
        - current_price: Current stock price
        - rows_total / rows_used / rows_excluded_stale / rows_excluded_undated /
          newest_opinion_date / window_months: 윈도우 메타데이터
    """
    anchor = (now or datetime.now(UTC)).date()
    cutoff = _months_before(anchor, window_months)

    surviving: list[dict[str, Any]] = []
    rows_excluded_stale = 0
    rows_excluded_undated = 0
    newest_opinion_date: date | None = None

    for op in opinions:
        parsed = _parse_opinion_date(op.get("date"))
        if parsed is None:
            rows_excluded_undated += 1
            continue
        if newest_opinion_date is None or parsed > newest_opinion_date:
            newest_opinion_date = parsed
        if parsed < cutoff:
            rows_excluded_stale += 1
            continue
        surviving.append(op)

    rating_counts: dict[str, int] = {"buy": 0, "hold": 0, "sell": 0}
    strong_buy_count = 0

    for op in surviving:
        rating_label = op.get("rating", op.get("rating_label", ""))
        normalized_label = normalize_rating_label(rating_label)
        rating_bucket = op.get("rating_bucket") or rating_to_bucket(normalized_label)

        if rating_bucket in rating_counts:
            rating_counts[rating_bucket] += 1

        if is_strong_buy(normalized_label):
            strong_buy_count += 1

    target_prices = [
        op["target_price"]
        for op in surviving
        if isinstance(op.get("target_price"), (int, float)) and op["target_price"] > 0
    ]

    consensus: dict[str, Any] = {
        "buy_count": rating_counts["buy"],
        "hold_count": rating_counts["hold"],
        "sell_count": rating_counts["sell"],
        "strong_buy_count": strong_buy_count,
        "total_count": len(surviving),
        "avg_target_price": None,
        "median_target_price": None,
        "min_target_price": None,
        "max_target_price": None,
        "upside_pct": None,
        "current_price": current_price,
        "rows_total": len(opinions),
        "rows_used": len(surviving),
        "rows_excluded_stale": rows_excluded_stale,
        "rows_excluded_undated": rows_excluded_undated,
        "newest_opinion_date": (
            newest_opinion_date.isoformat() if newest_opinion_date else None
        ),
        "window_months": window_months,
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
                (consensus["avg_target_price"] - current_price) / current_price * 100,
                2,
            )

    return consensus
