"""D3 v3.1 metrics and control-plane predicates."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from decimal import ROUND_CEILING, Decimal, localcontext

from research.kr_corpus.d3_engine.constants import DECIMAL_PRECISION


def locked_share_time_weighted_mean(daily_ratios: Sequence[Decimal]) -> Decimal:
    if not daily_ratios:
        return Decimal(0)
    if any(value < 0 or value > 1 for value in daily_ratios):
        raise ValueError("locked ratios must be in [0,1]")
    return sum(daily_ratios, Decimal(0)) / Decimal(len(daily_ratios))


def deployment_mean(
    *,
    daily_invested_cost: Sequence[Decimal],
    cumulative_contribution: Sequence[Decimal],
    initial_cash: Decimal,
) -> tuple[Decimal, tuple[Decimal, ...]]:
    if len(daily_invested_cost) != len(cumulative_contribution):
        raise ValueError("deployment series length mismatch")
    if not daily_invested_cost:
        return Decimal(0), ()
    ratios: list[Decimal] = []
    for invested, contribution in zip(
        daily_invested_cost, cumulative_contribution, strict=True
    ):
        denominator = initial_cash + contribution
        if denominator <= 0:
            raise ValueError("deployment denominator must be positive")
        ratios.append(invested / denominator)
    return sum(ratios, Decimal(0)) / len(ratios), tuple(ratios)


def nearest_rank(values: Iterable[int], percentile: Decimal) -> int:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("nearest-rank needs at least one value")
    if percentile <= 0 or percentile > 1:
        raise ValueError("percentile must be in (0,1]")
    rank = int((percentile * len(ordered)).to_integral_value(rounding=ROUND_CEILING))
    return ordered[rank - 1]


def twr_returns(
    *, start_unit_price: Decimal, end_unit_price: Decimal, calendar_days: Decimal
) -> tuple[Decimal, Decimal]:
    if min(start_unit_price, end_unit_price, calendar_days) <= 0:
        raise ValueError("TWR arguments must be positive")
    cumulative = end_unit_price / start_unit_price - Decimal(1)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        annualized = (end_unit_price / start_unit_price) ** (
            Decimal("365.2425") / calendar_days
        ) - Decimal(1)
    return cumulative, annualized


def dual_view_result(
    *,
    original_verdicts: Mapping[str, str],
    original_hard_guards: Mapping[str, Sequence[bool]],
    original_winner: str | None,
    clamp_verdicts: Mapping[str, str],
    clamp_hard_guards: Mapping[str, Sequence[bool]],
    clamp_winner: str | None,
) -> str:
    if (
        dict(original_verdicts) != dict(clamp_verdicts)
        or {key: tuple(value) for key, value in original_hard_guards.items()}
        != {key: tuple(value) for key, value in clamp_hard_guards.items()}
        or original_winner != clamp_winner
    ):
        return "INCONCLUSIVE_DATA_BIAS"
    return "CONSISTENT"


def virtual_exit_value(
    *, quantity: int, close: Decimal, sell_fee_rate: Decimal
) -> Decimal:
    gross = close * quantity
    return gross - gross * sell_fee_rate
