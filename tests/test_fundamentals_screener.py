# tests/test_fundamentals_screener.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.financial_fundamentals_snapshots.derive import FundamentalPeriod
from app.services.invest_view_model.fundamentals_screener import (
    PROFITABLE_COMPANY_SPEC,
    evaluate_fundamentals_candidates,
)


def _period(year: int, *, revenue, cost_of_sales, filing_date) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal(revenue),
        net_income=Decimal("100"),
        cost_of_sales=Decimal(cost_of_sales),
        discrete_revenue=Decimal(revenue),
        discrete_net_income=Decimal("100"),
    )


def test_includes_symbol_meeting_roe_and_gross_margin():
    valuation_rows = [
        {"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}
    ]
    periods = {
        "005930": [
            _period(
                2024,
                revenue="1000",
                cost_of_sales="700",
                filing_date=dt.date(2025, 3, 20),
            )
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={"005930": "삼성전자"},
    )
    # gross margin = (1000-700)/1000 = 0.30 >= 0.20, roe 20 >= 15 → included
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["gross_margin_ttm"] == 0.30


def test_excludes_when_gross_margin_below_threshold():
    valuation_rows = [
        {"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}
    ]
    periods = {
        "005930": [
            _period(
                2024,
                revenue="1000",
                cost_of_sales="900",
                filing_date=dt.date(2025, 3, 20),
            )
        ]
    }  # margin 0.10
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert excluded[0]["symbol"] == "005930" and "gross_margin" in excluded[0]["reason"]


def test_excludes_when_fundamentals_unavailable_never_silent_pass():
    valuation_rows = [
        {"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}
    ]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol={},  # no fundamentals
        spec=PROFITABLE_COMPANY_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert excluded[0]["reason"] == "gross_margin_ttm unavailable"


def test_pit_gate_excludes_unfiled_period():
    valuation_rows = [
        {"symbol": "005930", "roe": 20.0, "per": 8.0, "pbr": 1.2, "market_cap": 5e11}
    ]
    periods = {
        "005930": [
            _period(
                2024,
                revenue="1000",
                cost_of_sales="700",
                filing_date=dt.date(2025, 3, 20),
            )
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC,
        report_date=dt.date(2025, 1, 1),  # before filing
        limit=20,
        name_map={},
    )
    assert rows == []  # period not yet filed as of report_date → unavailable → excluded


def test_ranking_by_roe_desc_nulls_last_and_limit_trim():
    # All candidates pass the gross-margin gate (margin 0.30); ranking + trim is the SUT.
    roes = {"A": 50.0, "B": 10.0, "C": 30.0, "D": None, "E": 20.0, "F": 40.0}
    valuation_rows = [
        {"symbol": s, "roe": r, "per": 8.0, "pbr": 1.0, "market_cap": 1e11}
        for s, r in roes.items()
    ]
    periods = {
        s: [
            _period(
                2024,
                revenue="1000",
                cost_of_sales="700",
                filing_date=dt.date(2025, 3, 20),
            )
        ]
        for s in roes
    }

    # limit trims to the top-N by ROE descending (NULL-ROE sorts last → not in top 4)
    rows, _ = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=4,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["A", "F", "C", "E"]  # 50, 40, 30, 20

    # full set: the NULL-ROE candidate (D) sorts last, never silently dropped or first
    rows_all, _ = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=PROFITABLE_COMPANY_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows_all] == ["A", "F", "C", "E", "B", "D"]


def test_registry_has_four_specs_with_expected_thresholds():
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
    )

    assert set(FUNDAMENTALS_PRESET_SPECS) == {
        "profitable_company",
        "undervalued_growth",
        "stable_growth",
        "future_dividend_king",
    }
    ug = FUNDAMENTALS_PRESET_SPECS["undervalued_growth"]
    assert ug.max_per == Decimal("20") and ug.min_revenue_growth_3y_avg == Decimal("0.10")
    assert ug.min_earnings_growth_3y_avg == Decimal("0.20")
    sg = FUNDAMENTALS_PRESET_SPECS["stable_growth"]
    assert sg.min_roe == Decimal("15") and sg.min_earnings_growth_3y_avg == Decimal("0.10")
    assert sg.min_earnings_increase_streak_years == 3
    dk = FUNDAMENTALS_PRESET_SPECS["future_dividend_king"]
    assert dk.min_dividend_yield == Decimal("0.01") and dk.min_payout_ratio == Decimal("30")
    assert dk.min_dividend_growth_streak_years == 3 and dk.min_earnings_increase_streak_years == 3

