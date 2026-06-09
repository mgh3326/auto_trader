from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.financial_fundamentals_snapshots.derive import (
    FundamentalPeriod,
    derive_fundamentals_metrics,
)


def _annual(
    year: int,
    *,
    revenue,
    net_income,
    filing_date,
    gross_profit=None,
    cost_of_sales=None,
    payout_ratio=None,
    dps=None,
) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal(revenue) if revenue is not None else None,
        net_income=Decimal(net_income) if net_income is not None else None,
        gross_profit=Decimal(gross_profit) if gross_profit is not None else None,
        cost_of_sales=Decimal(cost_of_sales) if cost_of_sales is not None else None,
        discrete_revenue=Decimal(revenue) if revenue is not None else None,
        discrete_net_income=Decimal(net_income) if net_income is not None else None,
        payout_ratio=Decimal(payout_ratio) if payout_ratio is not None else None,
        dividend_per_share=Decimal(dps) if dps is not None else None,
        roe=None,
    )


def _periods():
    return [
        _annual(
            2021,
            revenue="1000",
            net_income="100",
            filing_date=dt.date(2022, 3, 20),
            dps="10",
            payout_ratio="20",
        ),
        _annual(
            2022,
            revenue="1100",
            net_income="120",
            filing_date=dt.date(2023, 3, 20),
            dps="11",
            payout_ratio="21",
        ),
        _annual(
            2023,
            revenue="1300",
            net_income="150",
            filing_date=dt.date(2024, 3, 20),
            dps="12",
            payout_ratio="22",
        ),
        _annual(
            2024,
            revenue="1600",
            net_income="200",
            filing_date=dt.date(2025, 3, 20),
            dps="13",
            payout_ratio="25",
        ),
    ]


def test_pit_gate_hides_unfiled_periods():
    # report_date before the 2024 filing → 2024 row invisible.
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2024, 12, 31))
    # latest visible payout = 2023 row (filed 2024-03-20)
    assert d.payout_ratio.value == Decimal("22")
    # after 2025-03-20 the 2024 row is visible
    d2 = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d2.payout_ratio.value == Decimal("25")


def test_growth_3y_avg_computed_when_four_years_visible():
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d.revenue_growth_3y_avg.state == "ok"
    assert d.earnings_growth_3y_avg.state == "ok"
    # YoY rev: 0.10, 0.1818..., 0.2308.. → avg ≈ 0.1709
    assert round(float(d.revenue_growth_3y_avg.value), 3) == 0.171


def test_earnings_increase_streak_counts_consecutive():
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert (
        d.earnings_increase_streak_years.value == 3
    )  # 2021<2022<2023<2024 → 3 increases


def test_dividend_streaks_missing_not_zero():
    periods = _periods()
    # Drop the 2023 dividend (None) → streak breaks, NOT counted as a 0-paid year.
    periods[2] = _annual(
        2023,
        revenue="1300",
        net_income="150",
        filing_date=dt.date(2024, 3, 20),
        dps=None,
        payout_ratio=None,
    )
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    # Most-recent consecutive paid run is just 2024 (2023 missing breaks it).
    assert d.dividend_paid_streak_years.value == 1


def test_gross_margin_partial_when_no_gross_profit_or_cogs():
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2025, 6, 1))
    assert d.gross_margin_ttm.state == "partial"
    assert d.gross_margin_ttm.value is None


def test_gross_margin_uses_cost_of_sales_fallback():
    periods = [
        _annual(
            2024,
            revenue="1000",
            net_income="100",
            filing_date=dt.date(2025, 3, 20),
            cost_of_sales="700",
        ),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    # gross margin = (1000 - 700) / 1000 = 0.30
    assert d.gross_margin_ttm.state == "ok"
    assert round(float(d.gross_margin_ttm.value), 2) == 0.30


def test_negative_base_year_makes_growth_partial():
    periods = [
        _annual(
            2023, revenue="1000", net_income="-50", filing_date=dt.date(2024, 3, 20)
        ),
        _annual(
            2024, revenue="1100", net_income="80", filing_date=dt.date(2025, 3, 20)
        ),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    assert d.earnings_growth_3y_avg.state in {"partial", "unavailable"}


def test_earnings_increase_streak_breaks_on_fiscal_year_gap():
    # 2021,2022 present then 2024 (2023 row absent) — the gap means the run ending
    # at 2024 has no contiguous prior year → streak 0 (NOT a fabricated 2).
    periods = [
        _annual(
            2021, revenue="1000", net_income="100", filing_date=dt.date(2022, 3, 20)
        ),
        _annual(
            2022, revenue="1100", net_income="120", filing_date=dt.date(2023, 3, 20)
        ),
        _annual(
            2024, revenue="1600", net_income="200", filing_date=dt.date(2025, 3, 20)
        ),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    assert d.earnings_increase_streak_years.value == 0


def test_dividend_streaks_unavailable_when_no_visible_periods():
    # report_date before every filing → 0 visible annual rows → dividend streaks
    # must be 'unavailable' (missing != zero; never (ok, 0)).
    d = derive_fundamentals_metrics(_periods(), report_date=dt.date(2020, 1, 1))
    assert d.dividend_paid_streak_years.state == "unavailable"
    assert d.dividend_growth_streak_years.state == "unavailable"
    assert d.earnings_increase_streak_years.state == "unavailable"


def test_dividend_paid_streak_breaks_on_fiscal_year_gap():
    # 2024 dividend present but 2023 dividend missing (None) → only 2024 counts.
    periods = _periods()
    periods[2] = _annual(
        2023,
        revenue="1300",
        net_income="150",
        filing_date=dt.date(2024, 3, 20),
        dps=None,
        payout_ratio=None,
    )
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 6, 1))
    assert d.dividend_paid_streak_years.value == 1


def _quarterly(
    year: int,
    quarter: int,
    *,
    net_income,
    filing_date,
    period_end_date=None,
) -> FundamentalPeriod:
    if period_end_date is None:
        period_end_date = {
            1: dt.date(year, 3, 31),
            2: dt.date(year, 6, 30),
            3: dt.date(year, 9, 30),
            4: dt.date(year, 12, 31),
        }[quarter]
    return FundamentalPeriod(
        fiscal_period=f"{year}Q{quarter}",
        period_type="quarterly",
        period_end_date=period_end_date,
        filing_date=filing_date,
        revenue=None,
        net_income=Decimal(net_income) if net_income is not None else None,
        gross_profit=None,
        cost_of_sales=None,
        discrete_revenue=None,
        discrete_net_income=Decimal(net_income) if net_income is not None else None,
        payout_ratio=None,
        dividend_per_share=None,
        roe=None,
    )


def test_earnings_growth_qoq_scenarios():
    # 1. Adjacent + fresh -> ok
    periods = [
        _quarterly(2025, 2, net_income="100", filing_date=dt.date(2025, 8, 14)),
        _quarterly(2025, 3, net_income="110", filing_date=dt.date(2025, 11, 14)),
    ]
    d = derive_fundamentals_metrics(periods, report_date=dt.date(2025, 11, 15))
    assert d.earnings_growth_qoq.state == "ok"
    assert round(float(d.earnings_growth_qoq.value), 2) == 0.10

    # 2. Non-adjacent (gap Q1 to Q3) -> unavailable
    periods_gap = [
        _quarterly(2025, 1, net_income="100", filing_date=dt.date(2025, 5, 15)),
        _quarterly(2025, 3, net_income="110", filing_date=dt.date(2025, 11, 14)),
    ]
    d_gap = derive_fundamentals_metrics(periods_gap, report_date=dt.date(2025, 11, 15))
    assert d_gap.earnings_growth_qoq.state == "unavailable"

    # 3. Stale latest quarter (> 183 days) -> unavailable
    d_stale = derive_fundamentals_metrics(periods, report_date=dt.date(2026, 6, 1))
    assert d_stale.earnings_growth_qoq.state == "unavailable"

    # 4. Non-positive base quarter -> partial
    periods_neg = [
        _quarterly(2025, 2, net_income="-50", filing_date=dt.date(2025, 8, 14)),
        _quarterly(2025, 3, net_income="10", filing_date=dt.date(2025, 11, 14)),
    ]
    d_neg = derive_fundamentals_metrics(periods_neg, report_date=dt.date(2025, 11, 15))
    assert d_neg.earnings_growth_qoq.state == "partial"
