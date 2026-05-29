"""Deterministic 0–10 candidate scoring curves (ROB-304).

Each curve is monotonic and documented so the report's
``score >= 7.0 → BULL`` branch is meaningful and reproducible."""

from __future__ import annotations


def clamp(value: float, low: float = 0.0, high: float = 10.0) -> float:
    return max(low, min(high, value))


def momentum_score(change_rate: float | None) -> float:
    """+10% → 10, 0% → 5, −10% → 0. ``None`` → 0."""
    if change_rate is None:
        return 0.0
    return clamp(5.0 + change_rate / 2.0)


def oversold_score(rsi: float | None) -> float:
    """Lower RSI → higher score. RSI 30 → 9, 50 → 5, 70 → 1. ``None`` → 0."""
    if rsi is None:
        return 0.0
    return clamp((50.0 - rsi) / 5.0 + 5.0)


def rank_score(index: int, count: int) -> float:
    """Rank-based score for batch metrics (volume). Best (index 0) → 10."""
    if count <= 1:
        return 10.0
    return clamp(10.0 * (1.0 - index / count))


def high_yield_value_score(roe: float | None, per: float | None) -> float:
    """ROE-led value score (ROB-363). Higher ROE and lower PER → higher score.

    Both qualify under the preset (ROE>=15, 0<PER<=10) so both contribute:
    ROE 15 → +0, ROE 35 → +5 (capped); PER 10 → +0, PER 0 → +5. ``None`` parts
    contribute 0. Result clamped 0–10."""
    roe_part = 0.0 if roe is None else clamp((roe - 15.0) / 4.0, 0.0, 5.0)
    per_part = 0.0 if per is None else clamp((10.0 - per) / 2.0, 0.0, 5.0)
    return clamp(roe_part + per_part)
