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


def _growth_period(year, *, revenue, net_income, filing_date):
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal(revenue),
        net_income=Decimal(net_income),
        discrete_revenue=Decimal(revenue),
        discrete_net_income=Decimal(net_income),
    )


def _four_growth_years(symbol, revs, nis):
    # revs/nis are 4 ascending-year values (2021..2024); filed the following March.
    return {
        symbol: [
            _growth_period(
                2021 + i,
                revenue=str(revs[i]),
                net_income=str(nis[i]),
                filing_date=dt.date(2022 + i, 3, 20),
            )
            for i in range(4)
        ]
    }


def test_stable_growth_includes_when_growth_and_streak_met():
    from app.services.invest_view_model.fundamentals_screener import STABLE_GROWTH_SPEC

    # net income 100→120→150→200 (all increases → streak 3; 3y-avg growth well above 10%)
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 20.0,
            "per": 9.0,
            "pbr": 1.1,
            "market_cap": 5e11,
            "dividend_yield": 0.02,
        }
    ]
    periods = _four_growth_years("005930", [1000, 1100, 1300, 1600], [100, 120, 150, 200])
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=STABLE_GROWTH_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["earnings_increase_streak_years"] == 3
    assert rows[0]["earnings_growth_3y_avg"] is not None


def test_stable_growth_excludes_when_streak_below_threshold():
    from app.services.invest_view_model.fundamentals_screener import STABLE_GROWTH_SPEC

    # net income dips in 2023 → streak ending 2024 is only 1 (< 3) → excluded.
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 20.0,
            "per": 9.0,
            "pbr": 1.1,
            "market_cap": 5e11,
            "dividend_yield": 0.02,
        }
    ]
    periods = _four_growth_years("005930", [1000, 1100, 1300, 1600], [100, 120, 90, 200])
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=STABLE_GROWTH_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert any("earnings_increase_streak_years" in e["reason"] for e in excluded)


def test_undervalued_growth_excludes_when_growth_metric_unavailable_never_silent():
    from app.services.invest_view_model.fundamentals_screener import (
        UNDERVALUED_GROWTH_SPEC,
    )

    # Only 1 annual period → 3y-avg growth is 'partial'/'unavailable' → excluded, not passed.
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 8.0,
            "per": 12.0,
            "pbr": 0.9,
            "market_cap": 3e11,
            "dividend_yield": 0.01,
        }
    ]
    periods = {
        "005930": [
            _growth_period(
                2024,
                revenue="1600",
                net_income="200",
                filing_date=dt.date(2025, 3, 20),
            )
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=UNDERVALUED_GROWTH_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert excluded  # never silently included


