"""ROB-1061 H3 (SS11.3, SS11.5, SS12.2) — pure, gap-restart-aware indicator
math over a trailing run of ``alpaca_track.daily_bars.DailyBar``.

EMA seed/update rule (AC6 requires this be "명시적으로 고정" — explicitly
fixed by this implementation; SS11.3 does not itself specify a seed
convention): the SMA of the first ``period`` closes seeds ``EMA_period``,
then each subsequent close updates it via the standard recursive form
``EMA_t = alpha*C_t + (1-alpha)*EMA_{t-1}``, ``alpha = 2/(period+1)``. This
is a deliberate, documented implementation choice (not a spec value being
relaxed) — flagged in the H3 completion report per the ROB-1061 instructions.

sigma20 annualization (SS11.5 says only "연율화 stdev", no explicit
trading-calendar convention): this module uses ``sqrt(365)`` (crypto trades
every calendar day, unlike the 252-trading-day equity convention) and SAMPLE
standard deviation (``ddof=1``) over the 20 daily log returns. Also a
documented implementation choice, flagged in the completion report.

Every indicator here is computed ONLY over a caller-supplied, ALREADY
gap-restart-segmented run of closes (``trailing_valid_segment``) — no
indicator ever silently spans an invalid/missing day (AC12); insufficient
history raises, it never forward-fills or reuses a stale value.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

__all__ = [
    "InsufficientPriceHistoryError",
    "SigmaInsufficientSampleError",
    "annualized_sigma20",
    "compute_momentum_r",
    "compute_score",
    "compute_trend_d",
    "ema_final_value",
    "trailing_valid_segment",
]

SIGMA_WINDOW_DAYS = 20
_ANNUALIZATION_DAYS = 365


class InsufficientPriceHistoryError(ValueError):
    """Not enough consecutive valid closes (since the last segment restart)
    to compute the requested indicator."""


class SigmaInsufficientSampleError(ValueError):
    """Not enough consecutive valid closes to compute the 20-day sigma
    (AC10: "sigma20 계산 불가(gap·표본 부족)면 진입 거절")."""


def trailing_valid_segment(bars: Sequence[object]) -> tuple[object, ...]:
    """The maximal SUFFIX of ``bars`` (already truncated to <= the decision
    boundary by the caller) that are all ``is_valid``, stopping the walk the
    instant a bar with ``is_segment_start=True`` has been included (that is
    the earliest data the CURRENT segment may use) or an invalid bar is hit.

    If the chronologically LAST bar itself is invalid (or ``bars`` is
    empty), returns ``()`` — there is no usable ``C_t`` for this decision at
    all (the caller must reject with ``INVALID_DECISION_DAY``).
    """
    result: list[object] = []
    for bar in reversed(bars):
        if not bar.is_valid:
            break
        result.append(bar)
        if bar.is_segment_start:
            break
    result.reverse()
    return tuple(result)


def ema_final_value(closes: Sequence[float], period: int) -> float:
    """The final value of an EMA(``period``) series seeded by the SMA of the
    first ``period`` closes (see module docstring for the seed-rule
    rationale). Raises ``InsufficientPriceHistoryError`` if
    ``len(closes) < period``."""
    if period <= 0:
        raise ValueError("period must be positive")
    if len(closes) < period:
        raise InsufficientPriceHistoryError(
            f"need >= {period} closes to seed EMA({period}), got {len(closes)}"
        )
    alpha = 2.0 / (period + 1)
    ema = math.fsum(closes[:period]) / period
    for close in closes[period:]:
        ema = alpha * close + (1.0 - alpha) * ema
    return ema


def compute_trend_d(closes: Sequence[float], *, f: int, s: int) -> float:
    """``D = EMA_f(C)/EMA_s(C) - 1`` over the trailing segment ``closes``
    (ascending, ``closes[-1] == C_t``). ``s > f`` always in the sealed grid,
    so the slow EMA's seed requirement (``len(closes) >= s``) is the binding
    constraint for this indicator alone."""
    ema_f = ema_final_value(closes, f)
    ema_s = ema_final_value(closes, s)
    return ema_f / ema_s - 1.0


def compute_momentum_r(closes: Sequence[float], *, m: int) -> float:
    """``R[m] = C[t]/C[t-m] - 1`` over the trailing segment ``closes``
    (ascending, ``closes[-1] == C_t``). Requires ``len(closes) >= m + 1``."""
    if m <= 0:
        raise ValueError("m must be positive")
    if len(closes) < m + 1:
        raise InsufficientPriceHistoryError(
            f"need >= {m + 1} closes for R[m={m}], got {len(closes)}"
        )
    c_t = closes[-1]
    c_t_minus_m = closes[-1 - m]
    return c_t / c_t_minus_m - 1.0


def compute_score(closes: Sequence[float], *, ell: int) -> float:
    """AP-A2's ``Score[L] = C[t]/C[t-L] - 1`` — mathematically identical
    lookback-return shape to ``compute_momentum_r``, kept as its own named
    entry point per SS12.2's own notation (``L`` vs ``m``)."""
    return compute_momentum_r(closes, m=ell)


def annualized_sigma20(closes: Sequence[float]) -> float:
    """The annualized sample stdev of the trailing 20 daily log returns
    (requires >= 21 consecutive closes in the same segment). See module
    docstring for the ``sqrt(365)``/sample-stdev convention rationale."""
    if len(closes) < SIGMA_WINDOW_DAYS + 1:
        raise SigmaInsufficientSampleError(
            f"need >= {SIGMA_WINDOW_DAYS + 1} closes for sigma20, got "
            f"{len(closes)}"
        )
    tail = closes[-(SIGMA_WINDOW_DAYS + 1) :]
    log_returns = [
        math.log(tail[i] / tail[i - 1]) for i in range(1, SIGMA_WINDOW_DAYS + 1)
    ]
    mean = math.fsum(log_returns) / SIGMA_WINDOW_DAYS
    variance = math.fsum((x - mean) ** 2 for x in log_returns) / (
        SIGMA_WINDOW_DAYS - 1
    )
    daily_sigma = math.sqrt(variance)
    if daily_sigma <= 0.0:
        raise SigmaInsufficientSampleError(
            "degenerate (zero-variance) close series — sigma20 undefined"
        )
    return daily_sigma * math.sqrt(_ANNUALIZATION_DAYS)
