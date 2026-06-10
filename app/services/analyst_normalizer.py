"""
Shared rating normalization and consensus building utilities for analyst data.

This module provides functions to normalize analyst rating labels to standard
English labels, classify them into aggregation buckets, and build consensus
statistics with extended fields.
"""

import calendar
from datetime import UTC, date, datetime
from typing import Any, Literal

# ROB-488: outlier guard thresholds for target-price aggregation.
# Corporate-action (액면분할/병합·감자) garbage shows up as absurd upside in
# either direction, but the math is asymmetric: upside is unbounded above yet
# floored at -100%, so the downside needs its own (tighter) threshold —
# abs(upside) > 300 can never fire below zero.
TARGET_PRICE_MAX_UPSIDE_PCT = 300.0
TARGET_PRICE_MIN_UPSIDE_PCT = -75.0
_OPINION_DATE_KEYS = ("date", "report_date", "published_date", "published_at", "datetime")

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


def _coerce_date(value: Any) -> date | None:
    """Coerce a value to a date, supporting str/datetime/date inputs."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    text = text[:10].replace(".", "-").replace("/", "-")
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _parse_opinion_date(value: Any) -> date | None:
    """행별 ISO date(YYYY-MM-DD[…]) 파싱 — _coerce_date 위임."""
    return _coerce_date(value)


def _opinion_date(opinion: dict[str, Any]) -> date | None:
    """Try multiple date-like keys in the opinion dict (ROB-488 compat)."""
    for key in _OPINION_DATE_KEYS:
        parsed = _coerce_date(opinion.get(key))
        if parsed is not None:
            return parsed
    return None


def _resolve_as_of_date(
    as_of: "date | datetime | str | None",
    now: "datetime | None",
    window_months: int,
) -> tuple[date, date]:
    """Return (anchor, cutoff) for opinion staleness check.

    Priority: explicit ``as_of`` > ``now`` kwarg > current UTC date.
    ``cutoff`` is anchor minus ``window_months`` months.
    """
    if as_of is not None:
        anchor = _coerce_date(as_of) or date.today()
    elif now is not None:
        anchor = now.date() if isinstance(now, datetime) else now
    else:
        anchor = datetime.now(UTC).date()
    cutoff = _months_before(anchor, window_months)
    return anchor, cutoff


def _is_stale_opinion(opinion: dict[str, Any], cutoff: date) -> bool:
    """Undated opinions are kept (fail-open, ROB-488): recency only excludes
    reports we can positively date past the cutoff; the outlier guard still
    covers undated corporate-action garbage when current_price is known."""
    observed = _opinion_date(opinion)
    if observed is None:
        return False
    return observed < cutoff


def _positive_target_price(opinion: dict[str, Any]) -> int | float | None:
    target = opinion.get("target_price")
    if isinstance(target, (int, float)) and target > 0:
        return target
    return None


def _is_target_price_outlier(
    target_price: int | float,
    current_price: int | float | None,
) -> bool:
    if not isinstance(current_price, (int, float)) or current_price <= 0:
        return False
    upside_pct = (target_price - current_price) / current_price * 100
    return (
        upside_pct > TARGET_PRICE_MAX_UPSIDE_PCT
        or upside_pct < TARGET_PRICE_MIN_UPSIDE_PCT
    )


def build_consensus(
    opinions: list[dict[str, Any]],
    current_price: int | float | None,
    *,
    window_months: int = 12,
    now: datetime | None = None,
    as_of: "date | datetime | str | None" = None,
) -> dict[str, Any]:
    """Build consensus statistics from analyst opinions within a recency window.

    Unified ROB-486 + ROB-488 implementation:
    - ``as_of`` (ROB-488 compat alias) or ``now`` (ROB-486) set the anchor date.
    - ``window_months`` (ROB-486, default 12) defines the staleness cutoff.
    - Undated opinions are kept (fail-open, ROB-488) but excluded from
      ``rows_excluded_undated`` metadata so callers can distinguish.
    - Outlier target prices (±300%/−75% vs current, ROB-488) are excluded
      from aggregates but counts are still credited.
    - Returns both ROB-486 metadata (rows_total/rows_used/rows_excluded_stale/
      rows_excluded_undated/newest_opinion_date/window_months) and ROB-488
      metadata (raw_count/stale_opinion_count/target_price_honest/
      target_price_count/target_price_outlier_count/target_price_excluded_count).

    Args:
        opinions: List of individual opinions with rating_bucket and target_price
        current_price: Current stock price
        window_months: Recency window in months (default 12, ROB-486)
        now: 집계 기준 시각 테스트 주입용 (ROB-486)
        as_of: Anchor date override (ROB-488 compat alias for now)

    Returns:
        Dictionary with consensus statistics.
    """
    _anchor, cutoff = _resolve_as_of_date(as_of, now, window_months)

    fresh_opinions: list[dict[str, Any]] = []
    stale_opinion_count = 0
    stale_target_price_count = 0
    rows_excluded_undated = 0
    newest_opinion_date: date | None = None

    for op in opinions:
        observed = _opinion_date(op)
        if observed is not None:
            if newest_opinion_date is None or observed > newest_opinion_date:
                newest_opinion_date = observed
        if _is_stale_opinion(op, cutoff):
            stale_opinion_count += 1
            if _positive_target_price(op) is not None:
                stale_target_price_count += 1
            continue
        # Track undated separately but keep them (fail-open, ROB-488)
        if observed is None:
            rows_excluded_undated += 1
        fresh_opinions.append(op)

    rating_counts: dict[str, int] = {"buy": 0, "hold": 0, "sell": 0}
    strong_buy_count = 0

    for op in fresh_opinions:
        rating_label = op.get("rating", op.get("rating_label", ""))
        normalized_label = normalize_rating_label(rating_label)
        rating_bucket = op.get("rating_bucket") or rating_to_bucket(normalized_label)

        if rating_bucket in rating_counts:
            rating_counts[rating_bucket] += 1

        if is_strong_buy(normalized_label):
            strong_buy_count += 1

    target_prices: list[int | float] = []
    target_price_outlier_count = 0
    for op in fresh_opinions:
        target_price = _positive_target_price(op)
        if target_price is None:
            continue
        if _is_target_price_outlier(target_price, current_price):
            target_price_outlier_count += 1
            continue
        target_prices.append(target_price)

    target_price_excluded_count = stale_target_price_count + target_price_outlier_count
    rows_excluded_stale = stale_opinion_count
    rows_used = len(fresh_opinions)

    consensus: dict[str, Any] = {
        "buy_count": rating_counts["buy"],
        "hold_count": rating_counts["hold"],
        "sell_count": rating_counts["sell"],
        "strong_buy_count": strong_buy_count,
        "total_count": len(fresh_opinions),
        "avg_target_price": None,
        "median_target_price": None,
        "min_target_price": None,
        "max_target_price": None,
        "upside_pct": None,
        "current_price": current_price,
        # ROB-486 metadata
        "rows_total": len(opinions),
        "rows_used": rows_used,
        "rows_excluded_stale": rows_excluded_stale,
        "rows_excluded_undated": rows_excluded_undated,
        "newest_opinion_date": (
            newest_opinion_date.isoformat() if newest_opinion_date else None
        ),
        "window_months": window_months,
        # ROB-488 metadata
        "raw_count": len(opinions),
        "stale_opinion_count": stale_opinion_count,
        "target_price_count": len(target_prices),
        "target_price_outlier_count": target_price_outlier_count,
        "target_price_excluded_count": target_price_excluded_count,
        "target_price_honest": target_price_excluded_count == 0,
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
