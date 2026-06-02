from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

MetricState = Literal["ok", "partial", "unavailable"]


@dataclass(frozen=True)
class FundamentalPeriod:
    fiscal_period: str
    period_type: str  # 'annual' | 'quarterly'
    period_end_date: dt.date
    filing_date: dt.date | None
    revenue: Decimal | None = None
    net_income: Decimal | None = None
    gross_profit: Decimal | None = None
    cost_of_sales: Decimal | None = None
    discrete_revenue: Decimal | None = None
    discrete_net_income: Decimal | None = None
    payout_ratio: Decimal | None = None
    dividend_per_share: Decimal | None = None
    roe: Decimal | None = None


@dataclass(frozen=True)
class MetricResult:
    value: Decimal | int | None
    state: MetricState
    note: str | None = None


@dataclass(frozen=True)
class FundamentalsDerivation:
    report_date: dt.date
    payout_ratio: MetricResult
    gross_margin_ttm: MetricResult
    revenue_growth_3y_avg: MetricResult
    earnings_growth_3y_avg: MetricResult
    earnings_growth_qoq: MetricResult
    earnings_increase_streak_years: MetricResult
    dividend_paid_streak_years: MetricResult
    dividend_growth_streak_years: MetricResult


_UNAVAILABLE = MetricResult(value=None, state="unavailable")


def _visible_annual(periods, report_date):
    rows = [
        p for p in periods
        if p.period_type == "annual"
        and p.filing_date is not None
        and p.filing_date <= report_date
    ]
    return sorted(rows, key=lambda p: p.period_end_date)


def _visible_quarterly(periods, report_date):
    rows = [
        p for p in periods
        if p.period_type == "quarterly"
        and p.filing_date is not None
        and p.filing_date <= report_date
    ]
    return sorted(rows, key=lambda p: p.period_end_date)


def _yoy(curr: Decimal | None, base: Decimal | None) -> Decimal | None:
    if curr is None or base is None or base <= 0:
        return None
    return (curr - base) / base


def _growth_3y_avg(values: list[Decimal | None]) -> MetricResult:
    # values are most-recent-last; need >=4 to form 3 YoY deltas.
    deltas: list[Decimal] = []
    undefined = False
    for i in range(1, len(values)):
        d = _yoy(values[i], values[i - 1])
        if d is None:
            undefined = True
        else:
            deltas.append(d)
    if not deltas:
        return _UNAVAILABLE
    avg = sum(deltas) / Decimal(len(deltas))
    if len(values) >= 4 and len(deltas) >= 3 and not undefined:
        return MetricResult(value=avg, state="ok")
    return MetricResult(value=avg, state="partial", note="fewer than 3 usable YoY deltas")


def _payout_ratio(annual: list) -> MetricResult:
    for p in reversed(annual):
        if p.payout_ratio is not None:
            return MetricResult(value=p.payout_ratio, state="ok")
    return _UNAVAILABLE


def _gross_margin_ttm(annual: list, quarterly: list) -> MetricResult:
    def margin(gross: Decimal | None, cogs: Decimal | None, rev: Decimal | None):
        if rev is None or rev <= 0:
            return None
        if gross is not None:
            return gross / rev
        if cogs is not None:
            return (rev - cogs) / rev
        return None

    # Prefer trailing-4-quarter TTM when available.
    if len(quarterly) >= 4:
        last4 = quarterly[-4:]
        rev = sum((q.discrete_revenue for q in last4 if q.discrete_revenue is not None), Decimal(0))
        gross_vals = [q.gross_profit for q in last4 if q.gross_profit is not None]
        cogs_vals = [q.cost_of_sales for q in last4 if q.cost_of_sales is not None]
        if rev > 0 and len(gross_vals) == 4:
            return MetricResult(value=sum(gross_vals, Decimal(0)) / rev, state="ok")
        if rev > 0 and len(cogs_vals) == 4:
            return MetricResult(value=(rev - sum(cogs_vals, Decimal(0))) / rev, state="ok")
    # Fall back to the latest annual figure.
    if annual:
        latest = annual[-1]
        m = margin(latest.gross_profit, latest.cost_of_sales, latest.revenue)
        if m is not None:
            return MetricResult(value=m, state="ok")
        return MetricResult(value=None, state="partial",
                            note="no gross profit / cost of sales (IFRS single-step)")
    return _UNAVAILABLE


def _earnings_growth_qoq(quarterly: list) -> MetricResult:
    usable = [q for q in quarterly if q.discrete_net_income is not None]
    if len(usable) < 2:
        return _UNAVAILABLE
    curr, prev = usable[-1].discrete_net_income, usable[-2].discrete_net_income
    g = _yoy(curr, prev)
    if g is None:
        return MetricResult(value=None, state="partial", note="non-positive base quarter")
    return MetricResult(value=g, state="ok")


def _increase_streak(values: list[Decimal | None]) -> MetricResult:
    # Count consecutive YoY increases ending at the most recent year.
    if len(values) < 2:
        return MetricResult(value=0, state="partial", note="insufficient history")
    streak = 0
    for i in range(len(values) - 1, 0, -1):
        a, b = values[i], values[i - 1]
        if a is None or b is None:
            break
        if a > b:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok")


def _dividend_paid_streak(annual: list) -> MetricResult:
    streak = 0
    for p in reversed(annual):
        dps = p.dividend_per_share
        if dps is None:          # missing != zero → cannot extend → stop
            break
        if dps > 0:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok")


def _dividend_growth_streak(annual: list) -> MetricResult:
    dps = [(p.period_end_date, p.dividend_per_share) for p in annual]
    dps = [d for d in dps if d[1] is not None]
    if len(dps) < 2:
        return MetricResult(value=0, state="partial", note="insufficient dividend history")
    streak = 0
    for i in range(len(dps) - 1, 0, -1):
        if dps[i][1] > dps[i - 1][1]:
            streak += 1
        else:
            break
    return MetricResult(value=streak, state="ok",
                        note="DPS is split/par-value unadjusted (DART raw)")


def derive_fundamentals_metrics(
    periods: list[FundamentalPeriod], *, report_date: dt.date
) -> FundamentalsDerivation:
    annual = _visible_annual(periods, report_date)
    quarterly = _visible_quarterly(periods, report_date)
    revenues = [p.revenue for p in annual]
    net_incomes = [p.net_income for p in annual]
    return FundamentalsDerivation(
        report_date=report_date,
        payout_ratio=_payout_ratio(annual),
        gross_margin_ttm=_gross_margin_ttm(annual, quarterly),
        revenue_growth_3y_avg=_growth_3y_avg(revenues) if revenues else _UNAVAILABLE,
        earnings_growth_3y_avg=_growth_3y_avg(net_incomes) if net_incomes else _UNAVAILABLE,
        earnings_growth_qoq=_earnings_growth_qoq(quarterly),
        earnings_increase_streak_years=_increase_streak(net_incomes) if net_incomes else _UNAVAILABLE,
        dividend_paid_streak_years=_dividend_paid_streak(annual),
        dividend_growth_streak_years=_dividend_growth_streak(annual),
    )
