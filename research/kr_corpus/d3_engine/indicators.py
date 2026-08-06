"""Decimal-only indicators fixed by the D3 v3.1 contract."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from research.kr_corpus.d3_engine.constants import (
    DECIMAL_PRECISION,
    DECIMAL_ROUNDING,
)

FIB_SUPPORT_RATIOS = (
    Decimal("0"),
    Decimal("0.236"),
    Decimal("0.382"),
    Decimal("0.5"),
    Decimal("0.618"),
)
FIB_RESISTANCE_RATIOS = (
    Decimal("0.236"),
    Decimal("0.382"),
    Decimal("0.5"),
    Decimal("0.618"),
    Decimal("1.0"),
)


@dataclass(frozen=True, slots=True)
class OhlcPoint:
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True, slots=True)
class BollingerBands:
    middle: Decimal
    lower: Decimal
    upper: Decimal


@dataclass(frozen=True, slots=True)
class FibWindow:
    start_index: int
    end_index: int
    excluded_index: int
    high: Decimal
    low: Decimal


def rsi_wilder(
    closes: Sequence[Decimal], period: int = 14
) -> tuple[Decimal | None, ...]:
    """Return Wilder RSI values, seeded from the first ``period`` deltas."""

    if period < 1:
        raise ValueError("period must be positive")
    if len(closes) < period + 1:
        return tuple(None for _ in closes)
    if any(not value.is_finite() or value <= 0 for value in closes):
        raise ValueError("RSI closes must be finite positive Decimals")

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        changes = [
            right - left for left, right in zip(closes, closes[1:], strict=False)
        ]
        gains = [max(change, Decimal(0)) for change in changes]
        losses = [max(-change, Decimal(0)) for change in changes]
        avg_gain = sum(gains[:period], Decimal(0)) / period
        avg_loss = sum(losses[:period], Decimal(0)) / period

        output: list[Decimal | None] = [None] * period
        output.append(_rsi_from_averages(avg_gain, avg_loss))
        for index in range(period, len(changes)):
            avg_gain = (avg_gain * (period - 1) + gains[index]) / period
            avg_loss = (avg_loss * (period - 1) + losses[index]) / period
            output.append(_rsi_from_averages(avg_gain, avg_loss))
        return tuple(output)


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal) -> Decimal:
    if avg_loss == 0:
        if avg_gain > 0:
            return Decimal(100)
        return Decimal(50)
    if avg_gain == 0:
        return Decimal(0)
    return Decimal(100) - Decimal(100) / (Decimal(1) + avg_gain / avg_loss)


def bollinger_bands(
    closes: Sequence[Decimal], *, window: int = 20, sigma: Decimal = Decimal("2")
) -> BollingerBands:
    """Population-standard-deviation Bollinger bands (ddof=0)."""

    if window < 1 or len(closes) < window:
        raise ValueError("insufficient closes for Bollinger window")
    values = tuple(closes[-window:])
    if any(not value.is_finite() or value <= 0 for value in values):
        raise ValueError("Bollinger closes must be finite positive Decimals")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        context.rounding = DECIMAL_ROUNDING
        mean = sum(values, Decimal(0)) / window
        variance = sum((value - mean) ** 2 for value in values) / window
        standard_deviation = variance.sqrt()
        return BollingerBands(
            middle=mean,
            lower=mean - sigma * standard_deviation,
            upper=mean + sigma * standard_deviation,
        )


def scan_fib_window(
    points: Sequence[OhlcPoint], *, decision_index: int, window: int = 120
) -> FibWindow:
    """Scan exactly the prior 120 sessions, excluding decision session ``t``."""

    if window != 120:
        raise ValueError("D3 fib window is fixed at 120")
    if decision_index < window or decision_index >= len(points):
        raise ValueError("decision index lacks 120 prior sessions or t bar")
    start = decision_index - window
    # Contract invariant: the half-open slice excludes points[decision_index] (t).
    selected = points[start:decision_index]
    if len(selected) != window:
        raise AssertionError("fib window length drift")
    if any(
        not point.high.is_finite()
        or not point.low.is_finite()
        or point.low <= 0
        or point.high < point.low
        for point in selected
    ):
        raise ValueError("invalid OHLC point in fib window")
    return FibWindow(
        start_index=start,
        end_index=decision_index - 1,
        excluded_index=decision_index,
        high=max(point.high for point in selected),
        low=min(point.low for point in selected),
    )


def fib_levels(
    low: Decimal,
    high: Decimal,
    ratios: Sequence[Decimal] = FIB_SUPPORT_RATIOS,
) -> dict[Decimal, Decimal]:
    if low <= 0 or high < low:
        raise ValueError("invalid fib extrema")
    spread = high - low
    return {ratio: low + ratio * spread for ratio in ratios}


def fib_resistance_above_close(
    low: Decimal, high: Decimal, close: Decimal
) -> dict[Decimal, Decimal]:
    return {
        ratio: level
        for ratio, level in fib_levels(low, high, FIB_RESISTANCE_RATIOS).items()
        if level > close
    }
