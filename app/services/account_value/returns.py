"""Flow-adjusted period returns without IO or implicit missing values."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal


@dataclass(frozen=True)
class Flow:
    day: date
    amount: Decimal


@dataclass(frozen=True)
class PeriodReturn:
    value: Decimal | None
    reason: str | None


def modified_dietz(
    start: date,
    end: date,
    daily_values: Mapping[date, Decimal | None],
    flows: Sequence[Flow],
) -> PeriodReturn:
    """Compute modified Dietz, treating each external flow as end-of-day.

    Values are closing valuations. A flow on the start day is already reflected
    in the opening valuation; flows after the opening and through the closing
    day are included. All calendar days must be present.
    """
    days = (end - start).days
    if days <= 0:
        return PeriodReturn(None, "invalid_period")
    if any(
        daily_values.get(start + timedelta(days=i)) is None for i in range(days + 1)
    ):
        return PeriodReturn(None, "missing_snapshot_day")
    values = [daily_values[start + timedelta(days=i)] for i in range(days + 1)]
    if any(value is None or not value.is_finite() for value in values):
        return PeriodReturn(None, "invalid_value")
    if any(not flow.amount.is_finite() for flow in flows):
        return PeriodReturn(None, "invalid_flow")
    opening = daily_values[start]
    closing = daily_values[end]
    assert opening is not None and closing is not None
    relevant = [flow for flow in flows if start < flow.day <= end]
    net_flow = sum((flow.amount for flow in relevant), Decimal(0))
    weighted = sum(
        (
            flow.amount * Decimal((end - flow.day).days) / Decimal(days)
            for flow in relevant
        ),
        Decimal(0),
    )
    capital = opening + weighted
    if capital <= 0:
        return PeriodReturn(None, "nonpositive_capital")
    return PeriodReturn((closing - opening - net_flow) / capital, None)


def benchmark_return(
    market: str, start_price: Decimal | None, end_price: Decimal | None
) -> PeriodReturn:
    """Return a benchmark only when its required total-return basis is known."""
    if market == "crypto":
        return PeriodReturn(Decimal(0), None)
    if market == "us":
        return PeriodReturn(None, "benchmark_total_return_unverified")
    if market != "kr":
        return PeriodReturn(None, "unknown_market")
    if start_price is None or end_price is None:
        return PeriodReturn(None, "benchmark_missing")
    if (
        not start_price.is_finite()
        or not end_price.is_finite()
        or start_price <= 0
        or end_price <= 0
    ):
        return PeriodReturn(None, "benchmark_invalid")
    return PeriodReturn(end_price / start_price - 1, None)
