"""Scoring functions for stock recommendations.

This module provides scoring functions that evaluate individual factors:
- RSI (Relative Strength Index) - lower RSI = better buy opportunity
- Valuation (PER/PBR) - lower ratios = better value
- Momentum (price change rate) - positive momentum = growth potential
- Volume - higher relative volume = more liquidity
- Dividend yield - higher yield = better income

All scores are normalized to 0-100 range where higher is better.
"""

from __future__ import annotations

from typing import Any


def _to_float(value: Any, default: float = 0.0) -> float:
    """Convert value to float with default fallback."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default


def _clamp_score(score: float) -> float:
    """Clamp score to 0-100 range."""
    return max(0.0, min(100.0, score))


def calc_rsi_score(rsi: float | None) -> float:
    """Calculate score from RSI value.

    Lower RSI = oversold = better buy opportunity = higher score.
    Scoring bands:
    - RSI <= 30: severely oversold, score 90-100
    - RSI 30-40: oversold, score 70-90
    - RSI 40-50: slightly oversold, score 50-70
    - RSI 50-60: neutral, score 40-50
    - RSI 60-70: slightly overbought, score 20-40
    - RSI > 70: overbought, score 0-20

    Args:
        rsi: RSI value (0-100) or None if unavailable

    Returns:
        Score 0-100 where higher is better (neutral = 50 if None)
    """
    if rsi is None:
        return 50.0  # Neutral score for missing data

    rsi = _to_float(rsi)
    if rsi <= 0:
        return 100.0
    if rsi <= 30:
        # Linear interpolation: RSI 0 -> 100, RSI 30 -> 90
        return _clamp_score(100.0 - (rsi / 30.0) * 10.0)
    if rsi <= 40:
        # Linear interpolation: RSI 30 -> 90, RSI 40 -> 70
        return _clamp_score(90.0 - ((rsi - 30) / 10.0) * 20.0)
    if rsi <= 50:
        # Linear interpolation: RSI 40 -> 70, RSI 50 -> 50
        return _clamp_score(70.0 - ((rsi - 40) / 10.0) * 20.0)
    if rsi <= 60:
        # Linear interpolation: RSI 50 -> 50, RSI 60 -> 40
        return _clamp_score(50.0 - ((rsi - 50) / 10.0) * 10.0)
    if rsi <= 70:
        # Linear interpolation: RSI 60 -> 40, RSI 70 -> 20
        return _clamp_score(40.0 - ((rsi - 60) / 10.0) * 20.0)
    # RSI > 70
    # Linear interpolation: RSI 70 -> 20, RSI 100 -> 0
    return _clamp_score(20.0 - ((rsi - 70) / 30.0) * 20.0)


def calc_valuation_score(per: float | None, pbr: float | None) -> float:
    """Calculate score from valuation metrics (PER/PBR).

    Lower PER/PBR = better value = higher score.
    Scoring bands for PER:
    - PER <= 5: extremely undervalued, score 90-100
    - PER 5-10: undervalued, score 70-90
    - PER 10-15: fair value, score 50-70
    - PER 15-25: slightly overvalued, score 30-50
    - PER > 25: overvalued, score 0-30

    PBR is used as a secondary factor to adjust the score.

    Args:
        per: Price-to-Earnings ratio or None
        pbr: Price-to-Book ratio or None

    Returns:
        Score 0-100 where higher is better (neutral = 50 if both None)
    """
    if per is None and pbr is None:
        return 50.0

    per_score = 50.0
    if per is not None:
        per = _to_float(per)
        if per <= 0:
            per_score = 50.0  # Negative PER (loss-making), neutral
        elif per <= 5:
            per_score = 95.0
        elif per <= 10:
            per_score = 80.0 - ((per - 5) / 5.0) * 10.0
        elif per <= 15:
            per_score = 60.0 - ((per - 10) / 5.0) * 10.0
        elif per <= 25:
            per_score = 40.0 - ((per - 15) / 10.0) * 10.0
        else:
            per_score = max(10.0, 30.0 - ((per - 25) / 25.0) * 20.0)

    pbr_score = 50.0
    if pbr is not None:
        pbr = _to_float(pbr)
        if pbr <= 0:
            pbr_score = 50.0
        elif pbr <= 0.5:
            pbr_score = 95.0
        elif pbr <= 1.0:
            pbr_score = 80.0 - ((pbr - 0.5) / 0.5) * 15.0
        elif pbr <= 2.0:
            pbr_score = 55.0 - ((pbr - 1.0) / 1.0) * 15.0
        else:
            pbr_score = max(10.0, 40.0 - ((pbr - 2.0) / 3.0) * 30.0)

    # Weighted average: PER is more important (60%), PBR is secondary (40%)
    if per is not None and pbr is not None:
        return _clamp_score(per_score * 0.6 + pbr_score * 0.4)
    elif per is not None:
        return _clamp_score(per_score)
    else:
        return _clamp_score(pbr_score)


def calc_momentum_score(change_rate: float | None) -> float:
    """Calculate score from price change rate (momentum).

    Positive momentum = growth potential = higher score.
    But extreme momentum can indicate overbought conditions.

    Scoring bands:
    - change_rate < -5%: oversold bounce potential, score 70-80
    - change_rate -5% to 0%: slight decline, score 50-70
    - change_rate 0% to 3%: positive momentum, score 60-80
    - change_rate 3% to 7%: strong momentum, score 75-90
    - change_rate > 7%: extreme momentum (caution), score 60-75

    Args:
        change_rate: Price change rate as percentage (e.g., 5.0 for +5%)

    Returns:
        Score 0-100 where higher is better (neutral = 50 if None)
    """
    if change_rate is None:
        return 50.0

    rate = _to_float(change_rate)

    if rate < -10:
        return 60.0
    if rate < -5:
        return 75.0 - ((rate + 10) / 5.0) * 15.0
    if rate < 0:
        return 60.0 - (rate / -5.0) * 10.0
    if rate < 3:
        return 65.0 + (rate / 3.0) * 15.0
    if rate < 7:
        return 80.0 + ((rate - 3) / 4.0) * 10.0
    # rate >= 7: caution zone
    return _clamp_score(90.0 - min(20.0, (rate - 7) * 2.0))


def calc_volume_score(volume: float | None, avg_volume: float | None = None) -> float:
    """Calculate score from trading volume.

    Higher relative volume = more liquidity and interest = higher score.
    If avg_volume is provided, uses relative volume; otherwise uses absolute volume bands.

    Args:
        volume: Current trading volume
        avg_volume: Average trading volume (for relative comparison)

    Returns:
        Score 0-100 where higher is better (neutral = 50 if None)
    """
    if volume is None:
        return 50.0

    volume = _to_float(volume)
    if volume <= 0:
        return 30.0

    # If we have average volume, use relative comparison
    if avg_volume is not None and avg_volume > 0:
        avg = _to_float(avg_volume)
        relative = volume / avg
        if relative < 0.5:
            return 40.0
        if relative < 1.0:
            return 50.0 + (relative - 0.5) * 20.0
        if relative < 2.0:
            return 60.0 + (relative - 1.0) * 20.0
        if relative < 5.0:
            return 80.0 + (relative - 2.0) * 6.67
        return 100.0

    # Without average, use absolute volume bands (log scale)
    # Assumes volume is in shares
    import math

    try:
        log_volume = math.log10(max(1, volume))
        # Log scale: 10^3 (1K) -> 30, 10^5 (100K) -> 50, 10^7 (10M) -> 70, 10^9 (1B) -> 90
        score = 10.0 + log_volume * 10.0
        return _clamp_score(score)
    except (ValueError, OverflowError):
        return 50.0


def calc_dividend_score(dividend_yield: float | None) -> float:
    """Calculate score from dividend yield.

    Higher dividend yield = better income = higher score.
    But extremely high yield can indicate distress.

    Scoring bands (yield as decimal, e.g., 0.03 for 3%):
    - yield < 1%: low yield, score 30-50
    - yield 1-2%: moderate yield, score 50-65
    - yield 2-4%: good yield, score 65-85
    - yield 4-6%: high yield, score 80-95
    - yield > 6%: very high (potential distress), score 70-85

    Args:
        dividend_yield: Dividend yield as decimal (0.03 = 3%) or percentage (3.0 = 3%)

    Returns:
        Score 0-100 where higher is better (neutral = 30 if None - no dividend)
    """
    if dividend_yield is None:
        return 30.0  # No dividend is neutral-negative for dividend strategy

    yield_val = _to_float(dividend_yield)

    # Handle percentage format (>1 means already in percentage)
    if yield_val > 1:
        yield_val = yield_val / 100.0

    if yield_val <= 0:
        return 20.0
    if yield_val < 0.01:  # < 1%
        return 40.0 + yield_val * 1000.0  # 0-1% -> 40-50
    if yield_val < 0.02:  # 1-2%
        return 50.0 + (yield_val - 0.01) * 1500.0  # 1-2% -> 50-65
    if yield_val < 0.04:  # 2-4%
        return 65.0 + (yield_val - 0.02) * 1000.0  # 2-4% -> 65-85
    if yield_val < 0.06:  # 4-6%
        return 85.0 + (yield_val - 0.04) * 500.0  # 4-6% -> 85-95
    # > 6%: caution zone
    return _clamp_score(95.0 - min(20.0, (yield_val - 0.06) * 200.0))


def calc_composite_score(
    item: dict[str, Any],
    rsi_weight: float = 0.20,
    valuation_weight: float = 0.25,
    momentum_weight: float = 0.25,
    volume_weight: float = 0.15,
    dividend_weight: float = 0.15,
) -> float:
    """Calculate composite score from multiple factors.

    Weights should sum to 1.0 for consistent scoring.

    Args:
        item: Stock data dictionary with keys:
            - rsi, rsi_14: RSI value (0-100)
            - per, pbr: Valuation metrics
            - change_rate: Price change percentage
            - volume: Trading volume
            - dividend_yield: Dividend yield (decimal or percentage)
        rsi_weight: Weight for RSI score (default 0.20)
        valuation_weight: Weight for valuation score (default 0.25)
        momentum_weight: Weight for momentum score (default 0.25)
        volume_weight: Weight for volume score (default 0.15)
        dividend_weight: Weight for dividend score (default 0.15)

    Returns:
        Composite score 0-100 where higher is better
    """
    # Extract values from item, handling multiple possible keys
    rsi = item.get("rsi") or item.get("rsi_14")
    per = item.get("per")
    pbr = item.get("pbr")
    change_rate = item.get("change_rate")
    volume = item.get("volume")
    dividend_yield = item.get("dividend_yield")

    # Calculate individual scores
    rsi_score = calc_rsi_score(rsi)
    valuation_score = calc_valuation_score(per, pbr)
    momentum_score = calc_momentum_score(change_rate)
    volume_score = calc_volume_score(volume)
    dividend_score = calc_dividend_score(dividend_yield)

    # Calculate weighted composite
    composite = (
        rsi_score * rsi_weight
        + valuation_score * valuation_weight
        + momentum_score * momentum_weight
        + volume_score * volume_weight
        + dividend_score * dividend_weight
    )

    return _clamp_score(composite)
