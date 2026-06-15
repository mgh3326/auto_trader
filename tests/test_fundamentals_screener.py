# tests/test_fundamentals_screener.py
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.financial_fundamentals_snapshots.derive import FundamentalPeriod
from app.services.invest_view_model.fundamentals_screener import (
    PROFITABLE_COMPANY_SPEC,
    FundamentalsPresetSpec,
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
    # Generic sort mechanism (roe desc, nulls last, limit trim) — uses a synthetic
    # roe-sort spec so it stays valid regardless of any real preset's sort_by
    # (ROB-432 changed profitable_company to gross_margin_ttm).
    spec = FundamentalsPresetSpec(
        preset_id="_test_roe_sort",
        min_gross_margin_ttm=Decimal("0.20"),
        sort_by="roe",
    )
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
        spec=spec,
        report_date=dt.date(2025, 6, 1),
        limit=4,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["A", "F", "C", "E"]  # 50, 40, 30, 20

    # full set: the NULL-ROE candidate (D) sorts last, never silently dropped or first
    rows_all, _ = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=spec,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows_all] == ["A", "F", "C", "E", "B", "D"]


def test_registry_has_nine_specs_with_expected_thresholds():
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
    )

    # ROB-428 PR-C: high_yield_value + undervalued_breakout (the last 2 KR Toss
    # valuation presets) were rerouted onto the tvscreener KR loader, so they now
    # live in this registry alongside the 7 fundamentals presets.
    assert set(FUNDAMENTALS_PRESET_SPECS) == {
        "profitable_company",
        "undervalued_growth",
        "stable_growth",
        "future_dividend_king",
        "cheap_value",
        "steady_dividend",
        "growth_expectation_toss",
        "high_yield_value",
        "undervalued_breakout",
    }
    ug = FUNDAMENTALS_PRESET_SPECS["undervalued_growth"]
    assert ug.max_per == Decimal("20") and ug.min_revenue_growth_3y_avg == Decimal(
        "0.10"
    )
    assert ug.min_earnings_growth_3y_avg == Decimal("0.20")
    sg = FUNDAMENTALS_PRESET_SPECS["stable_growth"]
    assert sg.min_roe == Decimal("15") and sg.min_earnings_growth_3y_avg == Decimal(
        "0.10"
    )
    assert sg.min_earnings_increase_streak_years == 3
    dk = FUNDAMENTALS_PRESET_SPECS["future_dividend_king"]
    assert dk.min_dividend_yield == Decimal("0.01") and dk.min_payout_ratio == Decimal(
        "30"
    )
    assert (
        dk.min_dividend_growth_streak_years == 3
        and dk.min_earnings_increase_streak_years == 3
    )
    # ROB-428 PR-C: replicate the OLD valuation loaders' thresholds exactly.
    hyv = FUNDAMENTALS_PRESET_SPECS["high_yield_value"]
    assert hyv.min_roe == Decimal("15") and hyv.max_per == Decimal("10")
    assert hyv.sort_by == "roe"
    assert hyv.max_new_high_age_trading_days is None  # not a breakout preset
    # ROB-430 PR-②: undervalued_breakout 신고가 = NEW 52w high within 20 trading days
    # (a breakout event), not price/52w-high proximity.
    ub = FUNDAMENTALS_PRESET_SPECS["undervalued_breakout"]
    assert ub.max_per == Decimal("10") and ub.max_pbr == Decimal("1")
    # ROB-432: Toss's "20 거래일" 신고가 window = 20 KRX trading sessions (XKRX),
    # replacing the earlier 30-calendar-day approximation.
    assert ub.max_new_high_age_trading_days == 20
    # ROB-432: Toss 저평가 탈출 default order = PER ascending (cheapest first).
    assert ub.sort_by == "per"
    assert ub.sort_descending is False


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
    periods = _four_growth_years(
        "005930", [1000, 1100, 1300, 1600], [100, 120, 150, 200]
    )
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
    periods = _four_growth_years(
        "005930", [1000, 1100, 1300, 1600], [100, 120, 90, 200]
    )
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


async def _cleanup_db(db_session):
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.market_valuation_snapshot import MarketValuationSnapshot

    for model in (
        FinancialFundamentalsSnapshot,
        MarketValuationSnapshot,
        KRSymbolUniverse,
    ):
        await db_session.execute(sa.delete(model).where(model.symbol.like("9064%")))
    await db_session.commit()


async def _isolated_snapshot_date(
    db_session,
    *,
    market: str = "kr",
    floor: dt.date = dt.date(2026, 6, 2),
) -> dt.date:
    """Use a partition newer than any persistent test DB residue."""
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot

    max_date = (
        await db_session.execute(
            sa.select(sa.func.max(MarketValuationSnapshot.snapshot_date)).where(
                MarketValuationSnapshot.market == market
            )
        )
    ).scalar_one_or_none()
    candidate = max(max_date or floor, floor) + dt.timedelta(days=7)
    while candidate.weekday() >= 5:
        candidate += dt.timedelta(days=1)
    return candidate


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_valuation_filter_max_per_excludes_high_per(db_session):
    await _cleanup_db(db_session)
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        UNDERVALUED_GROWTH_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = await _isolated_snapshot_date(db_session)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(["906401", "906402"])
        )
    )
    await db_session.commit()

    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="906401",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("15"),
                roe=Decimal("10"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("500000000000"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="906402",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("40"),
                roe=Decimal("10"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("400000000000"),
            ),  # PER 40 > 20 → excluded from candidates
        ]
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=UNDERVALUED_GROWTH_SPEC,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    # both lack fundamentals rows → both excluded from results, but candidate filtering
    # is observable via excluded list: only the PER<=20 symbol reaches derive (then excluded
    # for missing fundamentals); the PER>20 symbol never becomes a candidate.
    excluded_symbols = {e["symbol"] for e in result.excluded}
    assert "906401" in excluded_symbols
    assert "906402" not in excluded_symbols  # filtered at SQL candidate stage
    assert result.fundamentals_state == "missing"  # no fundamentals backfilled


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_us_candidate_filtering_and_fail_closed(db_session):
    # ROB-441 PR3: US runs the market-parameterized derive loader. Verify the US
    # candidate query (market=us) filters by the spec's valuation thresholds, and
    # that without US financial_fundamentals backfilled the candidates fail-closed
    # (reach derive then get excluded; never silently passed). The derive-inclusion
    # path is market-agnostic and covered by the KR + PR1 derive-reuse tests.
    await _cleanup_db(db_session)

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition,
    )
    from app.services.invest_view_model.fundamentals_screener import (
        UNDERVALUED_GROWTH_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    # seed on the US valuation partition the loader will resolve (shared DB safety).
    val_hp = await resolve_healthy_partition(
        db_session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="us",
    )
    vd = (
        val_hp.partition_date
        if (val_hp and val_hp.partition_date)
        else dt.date(2026, 6, 2)
    )
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="us",
                symbol="906451",
                snapshot_date=vd,
                source="yahoo",
                per=Decimal("15"),  # <= 20 → candidate
                roe=Decimal("20"),
                market_cap=Decimal("500000000000"),
            ),
            MarketValuationSnapshot(
                market="us",
                symbol="906452",
                snapshot_date=vd,
                source="yahoo",
                per=Decimal("40"),  # > 20 → filtered at SQL candidate stage
                roe=Decimal("20"),
                market_cap=Decimal("400000000000"),
            ),
        ]
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="us",
        spec=UNDERVALUED_GROWTH_SPEC,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    excluded_symbols = {e["symbol"] for e in result.excluded}
    assert (
        "906451" in excluded_symbols
    )  # candidate (per<=20) but no US ff → fail-closed
    assert "906452" not in excluded_symbols  # filtered at SQL candidate stage (per>20)
    await _cleanup_db(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_valuation_filter_dividend_yield_excludes_null(db_session):
    await _cleanup_db(db_session)
    # spec §7 item 2: min_dividend_yield candidate filter must drop NULL dividend_yield
    # rows (fail-closed). future_dividend_king sets min_dividend_yield=0.01.
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        FUTURE_DIVIDEND_KING_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = await _isolated_snapshot_date(db_session)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(["906411", "906412"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="906411",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("12"),
                roe=Decimal("10"),
                dividend_yield=Decimal("0.02"),
                market_cap=Decimal("500000000000"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="906412",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("12"),
                roe=Decimal("10"),
                dividend_yield=None,  # NULL → filtered
                market_cap=Decimal("400000000000"),
            ),
        ]
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=FUTURE_DIVIDEND_KING_SPEC,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    excluded_symbols = {e["symbol"] for e in result.excluded}
    assert (
        "906411" in excluded_symbols
    )  # dividend_yield 0.02 >= 0.01 → candidate (then no fundamentals)
    assert "906412" not in excluded_symbols  # NULL dividend_yield filtered at SQL stage


def _dividend_period(year, *, net_income, dps, payout_ratio, filing_date):
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal("1000"),
        net_income=Decimal(net_income),
        discrete_revenue=Decimal("1000"),
        discrete_net_income=Decimal(net_income),
        dividend_per_share=Decimal(dps),
        payout_ratio=Decimal(payout_ratio),
    )


def _four_dividend_years(symbol, nis, dpss, payouts):
    return {
        symbol: [
            _dividend_period(
                2021 + i,
                net_income=str(nis[i]),
                dps=str(dpss[i]),
                payout_ratio=str(payouts[i]),
                filing_date=dt.date(2022 + i, 3, 20),
            )
            for i in range(4)
        ]
    }


def _dividend_king_valuation(symbol="005930"):
    return [
        {
            "symbol": symbol,
            "roe": 12.0,
            "per": 11.0,
            "pbr": 1.0,
            "market_cap": 5e11,
            "dividend_yield": 0.02,
        }
    ]


def test_future_dividend_king_includes_when_all_gates_met():
    from app.services.invest_view_model.fundamentals_screener import (
        FUTURE_DIVIDEND_KING_SPEC,
    )

    # net income up 4y (streak 3); DPS up 4y (growth streak 3); payout latest 35 >= 30.
    periods = _four_dividend_years(
        "005930", [100, 120, 150, 200], [100, 110, 120, 130], [30, 32, 34, 35]
    )
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=_dividend_king_valuation(),
        periods_by_symbol=periods,
        spec=FUTURE_DIVIDEND_KING_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["dividend_growth_streak_years"] == 3
    assert rows[0]["earnings_increase_streak_years"] == 3
    assert rows[0]["payout_ratio"] == 35.0


def test_future_dividend_king_excludes_when_payout_below_threshold():
    from app.services.invest_view_model.fundamentals_screener import (
        FUTURE_DIVIDEND_KING_SPEC,
    )

    # Same as include but latest payout 25 < 30 → excluded with payout reason.
    periods = _four_dividend_years(
        "005930", [100, 120, 150, 200], [100, 110, 120, 130], [30, 32, 34, 25]
    )
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=_dividend_king_valuation(),
        periods_by_symbol=periods,
        spec=FUTURE_DIVIDEND_KING_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert any("payout_ratio" in e["reason"] for e in excluded)


def test_undervalued_growth_full_include_all_three_conditions():
    from app.services.invest_view_model.fundamentals_screener import (
        UNDERVALUED_GROWTH_SPEC,
    )

    # revenue +10%/yr (3y-avg 0.10+); net income +~20%/yr (3y-avg ~0.20+).
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
    periods = _four_growth_years(
        "005930", [1000, 1100, 1210, 1331], [100, 120, 144, 173]
    )
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=UNDERVALUED_GROWTH_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["revenue_growth_3y_avg"] >= 0.10
    assert rows[0]["earnings_growth_3y_avg"] >= 0.20


def test_sort_by_non_roe_key_orders_desc():
    # Generic non-roe-key desc sort mechanism — synthetic spec sorting by
    # earnings_growth_3y_avg, so it stays valid regardless of any real preset's
    # sort_by (ROB-432 changed undervalued_growth to revenue_growth_3y_avg).
    spec = FundamentalsPresetSpec(
        preset_id="_test_earnings_sort",
        max_per=Decimal("20"),
        min_revenue_growth_3y_avg=Decimal("0.10"),
        min_earnings_growth_3y_avg=Decimal("0.20"),
        sort_by="earnings_growth_3y_avg",
    )

    # Two qualifiers with different earnings growth; sort_by='earnings_growth_3y_avg' desc.
    valuation_rows = [
        {
            "symbol": "P",
            "roe": 8.0,
            "per": 12.0,
            "pbr": 0.9,
            "market_cap": 3e11,
            "dividend_yield": 0.01,
        },
        {
            "symbol": "Q",
            "roe": 8.0,
            "per": 12.0,
            "pbr": 0.9,
            "market_cap": 3e11,
            "dividend_yield": 0.01,
        },
    ]
    periods = {
        **_four_growth_years(
            "P", [1000, 1100, 1210, 1331], [100, 120, 144, 173]
        ),  # eg ~0.20
        **_four_growth_years(
            "Q", [1000, 1100, 1210, 1331], [100, 150, 225, 338]
        ),  # eg ~0.50
    }
    rows, _ = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=spec,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["Q", "P"]  # higher earnings_growth first


def _div_paid_period(year, *, net_income, dps, payout_ratio, filing_date):
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal("1000"),
        net_income=Decimal(net_income),
        discrete_revenue=Decimal("1000"),
        discrete_net_income=Decimal(net_income),
        dividend_per_share=Decimal(dps),
        payout_ratio=Decimal(payout_ratio),
    )


def test_pr2c1_registry_has_cheap_value_and_steady_dividend():
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
    )

    assert {"cheap_value", "steady_dividend"} <= set(FUNDAMENTALS_PRESET_SPECS)
    cv = FUNDAMENTALS_PRESET_SPECS["cheap_value"]
    assert cv.max_per == Decimal("15") and cv.max_pbr == Decimal("1.5")
    assert cv.min_earnings_growth_3y_avg == Decimal("0")
    sd = FUNDAMENTALS_PRESET_SPECS["steady_dividend"]
    assert sd.min_dividend_yield == Decimal("0.03") and sd.min_payout_ratio == Decimal(
        "30"
    )
    assert (
        sd.min_dividend_paid_streak_years == 3
        and sd.min_earnings_increase_streak_years == 3
    )


def test_steady_dividend_includes_when_all_dividend_gates_met():
    from app.services.invest_view_model.fundamentals_screener import (
        STEADY_DIVIDEND_SPEC,
    )

    # net income up 4y (increase streak 3); DPS > 0 each year (paid streak 3); payout latest 40.
    periods = {
        "005930": [
            _div_paid_period(
                2021 + i,
                net_income=str(ni),
                dps=str(d),
                payout_ratio="40",
                filing_date=dt.date(2022 + i, 3, 20),
            )
            for i, (ni, d) in enumerate([(100, 50), (120, 55), (150, 60), (200, 65)])
        ]
    }
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 9.0,
            "per": 8.0,
            "pbr": 1.0,
            "market_cap": 5e11,
            "dividend_yield": 0.04,
        }
    ]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=STEADY_DIVIDEND_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["dividend_paid_streak_years"] == 4


def test_steady_dividend_excludes_when_dividend_paid_streak_below_threshold():
    from app.services.invest_view_model.fundamentals_screener import (
        STEADY_DIVIDEND_SPEC,
    )

    # 2023 DPS = 0 → paid streak ending 2024 is only 1 (< 3) → excluded.
    periods = {
        "005930": [
            _div_paid_period(
                2021 + i,
                net_income=str(ni),
                dps=str(d),
                payout_ratio="40",
                filing_date=dt.date(2022 + i, 3, 20),
            )
            for i, (ni, d) in enumerate([(100, 50), (120, 55), (150, 0), (200, 65)])
        ]
    }
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 9.0,
            "per": 8.0,
            "pbr": 1.0,
            "market_cap": 5e11,
            "dividend_yield": 0.04,
        }
    ]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=STEADY_DIVIDEND_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert any("dividend_paid_streak_years" in e["reason"] for e in excluded)


def test_cheap_value_includes_when_earnings_growth_non_negative():
    from app.services.invest_view_model.fundamentals_screener import CHEAP_VALUE_SPEC

    # revenue flat-ish, net income non-decreasing → earnings_growth_3y_avg >= 0.
    periods = _four_growth_years(
        "005930", [1000, 1010, 1020, 1030], [100, 100, 110, 120]
    )
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 5.0,
            "per": 10.0,
            "pbr": 0.8,
            "market_cap": 3e11,
            "dividend_yield": 0.01,
        }
    ]
    rows, _ = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=CHEAP_VALUE_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert [r["symbol"] for r in rows] == ["005930"]


def test_cheap_value_excludes_when_earnings_growth_negative():
    from app.services.invest_view_model.fundamentals_screener import CHEAP_VALUE_SPEC

    # net income declining → earnings_growth_3y_avg < 0 → excluded.
    periods = _four_growth_years(
        "005930", [1000, 1010, 1020, 1030], [200, 180, 150, 120]
    )
    valuation_rows = [
        {
            "symbol": "005930",
            "roe": 5.0,
            "per": 10.0,
            "pbr": 0.8,
            "market_cap": 3e11,
            "dividend_yield": 0.01,
        }
    ]
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=CHEAP_VALUE_SPEC,
        report_date=dt.date(2025, 6, 1),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert any("earnings_growth_3y_avg" in e["reason"] for e in excluded)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_valuation_filter_max_pbr_excludes_high_pbr(db_session):
    await _cleanup_db(db_session)
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        CHEAP_VALUE_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = await _isolated_snapshot_date(db_session)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(["906421", "906422"])
        )
    )
    await db_session.commit()
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="906421",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("12"),
                pbr=Decimal("1.0"),
                roe=Decimal("8"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("500000000000"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="906422",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("12"),
                pbr=Decimal("3.0"),  # PBR 3.0 > 1.5 → excluded
                roe=Decimal("8"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("400000000000"),
            ),
        ]
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=CHEAP_VALUE_SPEC,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    excluded_symbols = {e["symbol"] for e in result.excluded}
    assert (
        "906421" in excluded_symbols
    )  # PBR 1.0 <= 1.5 → candidate (then no fundamentals)
    assert "906422" not in excluded_symbols  # PBR 3.0 filtered at SQL stage


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_dedups_symbol_across_multiple_sources(db_session):
    await _cleanup_db(db_session)
    import sqlalchemy as sa

    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        CHEAP_VALUE_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = await _isolated_snapshot_date(db_session)
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol == "906431"
        )
    )
    await db_session.commit()
    # Same KR symbol under two sources at the same date (defensive — KR is single-source today).
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol="906431",
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("10"),
                pbr=Decimal("1.0"),
                roe=Decimal("8"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("500000000000"),
            ),
            MarketValuationSnapshot(
                market="kr",
                symbol="906431",
                snapshot_date=vd,
                source="yahoo",
                per=Decimal("10"),
                pbr=Decimal("1.0"),
                roe=Decimal("8"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("500000000000"),
            ),
        ]
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=CHEAP_VALUE_SPEC,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    # 906431 reaches derive at most ONCE (deduped), not twice.
    assert [e["symbol"] for e in result.excluded].count("906431") == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_loader_end_to_end_includes_and_excludes_with_fundamentals(db_session):
    await _cleanup_db(db_session)
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        CHEAP_VALUE_SPEC,
        load_fundamentals_preset_from_snapshots,
    )

    vd = await _isolated_snapshot_date(db_session)
    syms = ["906441", "906442"]
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.commit()
    # Both pass valuation (PER 12<=15, PBR 1.0<=1.5).
    db_session.add_all(
        [
            MarketValuationSnapshot(
                market="kr",
                symbol=s,
                snapshot_date=vd,
                source="naver_finance",
                per=Decimal("12"),
                pbr=Decimal("1.0"),
                roe=Decimal("8"),
                dividend_yield=Decimal("0.01"),
                market_cap=Decimal("500000000000"),
            )
            for s in syms
        ]
    )
    # 906441 earnings growing (eg >= 0 → included); 906442 declining (eg < 0 → excluded).
    for s, nis in [("906441", [100, 110, 120, 130]), ("906442", [200, 180, 150, 120])]:
        for i, ni in enumerate(nis):
            db_session.add(
                FinancialFundamentalsSnapshot(
                    market="kr",
                    symbol=s,
                    fiscal_period=f"{2021 + i}A",
                    period_type="annual",
                    period_end_date=dt.date(2021 + i, 12, 31),
                    filing_date=dt.date(2022 + i, 3, 20),
                    effective_at=dt.date(2022 + i, 3, 20),
                    source="dart",
                    source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
                    revenue=Decimal("1000"),
                    net_income=Decimal(ni),
                    data_state="fresh",
                )
            )
    db_session.add_all(
        [
            KRSymbolUniverse(
                symbol=s, name=f"종목{s}", exchange="KOSPI", is_active=True
            )
            for s in syms
        ]
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=CHEAP_VALUE_SPEC,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None
    assert [r["symbol"] for r in result.rows] == ["906441"]  # growing earnings included
    assert result.fundamentals_state == "fresh"  # fundamentals rows exist
    assert "906442" in {e["symbol"] for e in result.excluded}  # declining → excluded


def _annual_period(year: int, *, net_income, filing_date) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=f"{year}A",
        period_type="annual",
        period_end_date=dt.date(year, 12, 31),
        filing_date=filing_date,
        revenue=Decimal("1000"),
        net_income=Decimal(net_income),
        gross_profit=None,
        cost_of_sales=None,
        discrete_revenue=Decimal("1000"),
        discrete_net_income=Decimal(net_income),
    )


def _quarterly_period(
    year: int, quarter: int, *, net_income, filing_date
) -> FundamentalPeriod:
    return FundamentalPeriod(
        fiscal_period=f"{year}Q{quarter}",
        period_type="quarterly",
        period_end_date={
            1: dt.date(year, 3, 31),
            2: dt.date(year, 6, 30),
            3: dt.date(year, 9, 30),
            4: dt.date(year, 12, 31),
        }[quarter],
        filing_date=filing_date,
        revenue=Decimal("1000"),
        net_income=Decimal(net_income),
        gross_profit=None,
        cost_of_sales=None,
        discrete_revenue=Decimal("250"),
        discrete_net_income=Decimal(net_income),
    )


def test_toss_growth_expectation_includes_matching_symbols():
    from app.services.invest_view_model.fundamentals_screener import (
        GROWTH_EXPECTATION_TOSS_SPEC,
    )

    valuation_rows = [{"symbol": "005930", "roe": 10.0}]

    periods = {
        "005930": [
            _annual_period(2021, net_income="100", filing_date=dt.date(2022, 3, 20)),
            _annual_period(2022, net_income="110", filing_date=dt.date(2023, 3, 20)),
            _annual_period(2023, net_income="120", filing_date=dt.date(2024, 3, 20)),
            _annual_period(2024, net_income="130", filing_date=dt.date(2025, 3, 20)),
            _quarterly_period(
                2025, 2, net_income="100", filing_date=dt.date(2025, 8, 14)
            ),
            _quarterly_period(
                2025, 3, net_income="110", filing_date=dt.date(2025, 11, 14)
            ),
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=GROWTH_EXPECTATION_TOSS_SPEC,
        report_date=dt.date(2025, 11, 15),
        limit=20,
        name_map={"005930": "삼성전자"},
    )
    assert [r["symbol"] for r in rows] == ["005930"]
    assert rows[0]["earnings_growth_qoq"] == 0.10


def test_toss_growth_expectation_excludes_when_qoq_growth_insufficient():
    from app.services.invest_view_model.fundamentals_screener import (
        GROWTH_EXPECTATION_TOSS_SPEC,
    )

    valuation_rows = [{"symbol": "005930", "roe": 10.0}]

    periods = {
        "005930": [
            _annual_period(2021, net_income="100", filing_date=dt.date(2022, 3, 20)),
            _annual_period(2022, net_income="110", filing_date=dt.date(2023, 3, 20)),
            _annual_period(2023, net_income="120", filing_date=dt.date(2024, 3, 20)),
            _annual_period(2024, net_income="130", filing_date=dt.date(2025, 3, 20)),
            _quarterly_period(
                2025, 2, net_income="100", filing_date=dt.date(2025, 8, 14)
            ),
            _quarterly_period(
                2025, 3, net_income="105", filing_date=dt.date(2025, 11, 14)
            ),
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=GROWTH_EXPECTATION_TOSS_SPEC,
        report_date=dt.date(2025, 11, 15),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert excluded[0]["symbol"] == "005930"
    assert "earnings_growth_qoq" in excluded[0]["reason"]


def test_toss_growth_expectation_excludes_when_3y_avg_growth_insufficient():
    from app.services.invest_view_model.fundamentals_screener import (
        GROWTH_EXPECTATION_TOSS_SPEC,
    )

    valuation_rows = [{"symbol": "005930", "roe": 10.0}]

    periods = {
        "005930": [
            _annual_period(2021, net_income="100", filing_date=dt.date(2022, 3, 20)),
            _annual_period(2022, net_income="95", filing_date=dt.date(2023, 3, 20)),
            _annual_period(2023, net_income="90", filing_date=dt.date(2024, 3, 20)),
            _annual_period(2024, net_income="85", filing_date=dt.date(2025, 3, 20)),
            _quarterly_period(
                2025, 2, net_income="100", filing_date=dt.date(2025, 8, 14)
            ),
            _quarterly_period(
                2025, 3, net_income="110", filing_date=dt.date(2025, 11, 14)
            ),
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=GROWTH_EXPECTATION_TOSS_SPEC,
        report_date=dt.date(2025, 11, 15),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert excluded[0]["symbol"] == "005930"
    assert "earnings_growth_3y_avg" in excluded[0]["reason"]


def test_toss_growth_expectation_fail_closed_on_missing_quarterly_data():
    from app.services.invest_view_model.fundamentals_screener import (
        GROWTH_EXPECTATION_TOSS_SPEC,
    )

    valuation_rows = [{"symbol": "005930", "roe": 10.0}]

    periods = {
        "005930": [
            _annual_period(2021, net_income="100", filing_date=dt.date(2022, 3, 20)),
            _annual_period(2022, net_income="110", filing_date=dt.date(2023, 3, 20)),
            _annual_period(2023, net_income="120", filing_date=dt.date(2024, 3, 20)),
            _annual_period(2024, net_income="130", filing_date=dt.date(2025, 3, 20)),
        ]
    }
    rows, excluded = evaluate_fundamentals_candidates(
        valuation_rows=valuation_rows,
        periods_by_symbol=periods,
        spec=GROWTH_EXPECTATION_TOSS_SPEC,
        report_date=dt.date(2025, 11, 15),
        limit=20,
        name_map={},
    )
    assert rows == []
    assert excluded[0]["symbol"] == "005930"
    assert "earnings_growth_qoq unavailable" in excluded[0]["reason"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_us_dividend_yield_displayed_as_percent(db_session):
    # ROB-440: market_valuation stores dividend_yield as a RATIO (0.05); the screener
    # formatter expects PERCENT. US display rows must be ×100 (0.05 → 5.0) so the
    # dividend label shows "5.00%" not "0.05%". The SQL filter ran on the raw ratio.
    await _cleanup_db(db_session)
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_screener_snapshots.partition_health import (
        resolve_healthy_partition,
    )
    from app.services.invest_view_model.fundamentals_screener import (
        FundamentalsPresetSpec,
        load_fundamentals_preset_from_snapshots,
    )

    sym = "906470"
    val_hp = await resolve_healthy_partition(
        db_session,
        model=MarketValuationSnapshot,
        date_col=MarketValuationSnapshot.snapshot_date,
        market_col=MarketValuationSnapshot.market,
        market="us",
    )
    vd = (
        val_hp.partition_date
        if (val_hp and val_hp.partition_date)
        else dt.date(2026, 6, 5)
    )
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol=sym,
            snapshot_date=vd,
            source="yahoo",
            dividend_yield=Decimal("0.05"),  # ratio (= 5%)
            market_cap=Decimal("500000000000"),
        )
    )
    await db_session.commit()

    # dividend-only spec (no derive checks → no financial_fundamentals needed)
    spec = FundamentalsPresetSpec(
        preset_id="_test_div",
        min_dividend_yield=Decimal("0.01"),
        sort_by="dividend_yield",
    )
    res = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="us",
        spec=spec,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 5, tzinfo=dt.UTC),
    )
    assert res is not None
    row = next(r for r in res.rows if r["symbol"] == sym)
    assert row["dividend_yield"] == 5.0  # 0.05 ratio ×100 → 5.0 percent (display)
    # ROB-440: row market is the real market (was hardcoded "kr" by evaluate) so the
    # screener renders market_cap as USD, not 억원.
    assert row["market"] == "us"
    from app.services.invest_view_model.screener_service import _format_market_cap

    label, _ = _format_market_cap(row, row["market"])
    assert label.startswith("$")  # USD formatter (was "...억원" with the "kr" bug)
    await _cleanup_db(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kr_dividend_yield_not_double_scaled(db_session):
    # KR via this loader (reports/PIT) keeps the raw ratio — only US display is ×100.
    await _cleanup_db(db_session)
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        FundamentalsPresetSpec,
        load_fundamentals_preset_from_snapshots,
    )

    sym = "906471"
    vd = await _isolated_snapshot_date(db_session)
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol=sym,
            snapshot_date=vd,
            source="naver_finance",
            dividend_yield=Decimal("0.05"),
            market_cap=Decimal("500000000000"),
        )
    )
    db_session.add(
        KRSymbolUniverse(
            symbol=sym, name="배당테스트", exchange="KOSPI", is_active=True
        )
    )
    await db_session.commit()
    spec = FundamentalsPresetSpec(
        preset_id="_test_div_kr",
        min_dividend_yield=Decimal("0.01"),
        sort_by="dividend_yield",
    )
    res = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=spec,
        limit=20,
        now=lambda: dt.datetime(2026, 6, 4, tzinfo=dt.UTC),
    )
    assert res is not None
    row = next(r for r in res.rows if r["symbol"] == sym)
    assert row["dividend_yield"] == 0.05  # KR unchanged (no ×100)
    await _cleanup_db(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_us_quality_guards_drop_microcap_and_bad_data(db_session):
    # ROB-440: US fundamentals quality guards — micro-cap (size floor) + ROE/dividend
    # sanity caps drop the yahoo outliers the corrected labels exposed. KR unaffected.
    await _cleanup_db(db_session)
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        FundamentalsPresetSpec,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2099, 12, 1)

    def _mv(symbol, *, market, roe=None, per=None, div=None, mcap):
        return MarketValuationSnapshot(
            market=market,
            symbol=symbol,
            snapshot_date=vd,
            source="yahoo",
            roe=roe,
            per=per,
            dividend_yield=div,
            market_cap=Decimal(str(mcap)),
        )

    db_session.add_all(
        [
            _mv(
                "DCX",
                market="us",
                roe=Decimal("1177"),
                per=Decimal("5"),
                mcap=48_000_000,
            ),  # micro + ROE artifact
            _mv(
                "BLKB",
                market="us",
                roe=Decimal("418"),
                per=Decimal("5"),
                mcap=1_300_000_000,
            ),  # large-cap ROE artifact
            _mv(
                "GOODR",
                market="us",
                roe=Decimal("25"),
                per=Decimal("8"),
                mcap=190_000_000_000,
            ),  # legit
        ]
    )
    await db_session.commit()
    now = lambda: dt.datetime(2099, 12, 1, tzinfo=dt.UTC)  # noqa: E731

    roe_spec = FundamentalsPresetSpec(
        preset_id="_t_roe", min_roe=Decimal("15"), sort_by="roe"
    )
    res = await load_fundamentals_preset_from_snapshots(
        db_session, market="us", spec=roe_spec, limit=20, now=now
    )
    syms = {r["symbol"] for r in res.rows}
    assert syms == {"GOODR"}  # DCX(size floor) + BLKB(ROE cap) dropped

    # dividend cap: bad-data NVO-like dropped, legit high-yield kept
    await db_session.execute(
        MarketValuationSnapshot.__table__.delete().where(
            MarketValuationSnapshot.snapshot_date == vd
        )
    )
    db_session.add_all(
        [
            _mv(
                "BADDIV", market="us", div=Decimal("0.2674"), mcap=190_000_000_000
            ),  # yahoo artifact
            _mv(
                "AGNCX", market="us", div=Decimal("0.14"), mcap=12_000_000_000
            ),  # legit ~14%
        ]
    )
    await db_session.commit()
    div_spec = FundamentalsPresetSpec(
        preset_id="_t_div", min_dividend_yield=Decimal("0.03"), sort_by="dividend_yield"
    )
    res2 = await load_fundamentals_preset_from_snapshots(
        db_session, market="us", spec=div_spec, limit=20, now=now
    )
    assert {r["symbol"] for r in res2.rows} == {"AGNCX"}  # BADDIV(>25%) dropped

    await db_session.execute(
        MarketValuationSnapshot.__table__.delete().where(
            MarketValuationSnapshot.snapshot_date == vd
        )
    )
    await db_session.commit()
    await _cleanup_db(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_kr_unaffected_by_us_quality_guards(db_session):
    # KR path (reports/PIT) must NOT apply the US-only guards.
    await _cleanup_db(db_session)
    from app.models.kr_symbol_universe import KRSymbolUniverse
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.services.invest_view_model.fundamentals_screener import (
        FundamentalsPresetSpec,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2099, 12, 2)
    # idempotent on the persistent local DB (prior runs may have left these)
    await db_session.execute(
        MarketValuationSnapshot.__table__.delete().where(
            MarketValuationSnapshot.snapshot_date == vd
        )
    )
    await db_session.execute(
        KRSymbolUniverse.__table__.delete().where(KRSymbolUniverse.symbol == "900001")
    )
    db_session.add(
        MarketValuationSnapshot(
            market="kr",
            symbol="900001",
            snapshot_date=vd,
            source="naver_finance",
            roe=Decimal("1177"),
            per=Decimal("5"),
            market_cap=Decimal("48000000"),
        )
    )
    db_session.add(
        KRSymbolUniverse(
            symbol="900001", name="마이크로", exchange="KOSDAQ", is_active=True
        )
    )
    await db_session.commit()
    res = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="kr",
        spec=FundamentalsPresetSpec(
            preset_id="_t_kr", min_roe=Decimal("15"), sort_by="roe"
        ),
        limit=20,
        now=lambda: dt.datetime(2099, 12, 2, tzinfo=dt.UTC),
    )
    assert "900001" in {
        r["symbol"] for r in res.rows
    }  # KR micro-cap kept (no US guard)

    await db_session.execute(
        MarketValuationSnapshot.__table__.delete().where(
            MarketValuationSnapshot.snapshot_date == vd
        )
    )
    await db_session.execute(
        KRSymbolUniverse.__table__.delete().where(KRSymbolUniverse.symbol == "900001")
    )
    await db_session.commit()
    await _cleanup_db(db_session)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_us_fundamentals_rows_hydrate_price_from_invest_screener(
    db_session,
) -> None:
    """ROB-508: US 펀더멘털 행은 최신 invest_screener_snapshots 파티션에서
    close/change_rate/volume을 hydrate해야 한다 (priceLabel "-" 공백 해소)."""
    await _cleanup_db(db_session)
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
    from app.models.invest_screener_snapshot import InvestScreenerSnapshot
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.models.us_symbol_universe import USSymbolUniverse
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2026, 6, 2)
    syms = ["AAPL"]

    # 1. Clean up
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(InvestScreenerSnapshot.symbol.in_(syms))
    )
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()

    # 2. Add US Symbol
    db_session.add(
        USSymbolUniverse(
            symbol="AAPL",
            name_en="Apple Inc.",
            name_kr="애플",
            exchange="NASDAQ",
            is_active=True,
            is_common_stock=True,
        )
    )

    # 3. Add MarketValuationSnapshot
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol="AAPL",
            snapshot_date=vd,
            source="yahoo",
            per=Decimal("15"),
            roe=Decimal("20"),
            market_cap=Decimal("3000000000000"),
        )
    )

    # 4. Add FinancialFundamentalsSnapshot
    db_session.add(
        FinancialFundamentalsSnapshot(
            market="us",
            symbol="AAPL",
            fiscal_period="2024A",
            period_type="annual",
            period_end_date=dt.date(2024, 12, 31),
            filing_date=dt.date(2025, 3, 20),
            effective_at=dt.date(2025, 3, 20),
            source="yfinance",
            source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
            revenue=Decimal("1000"),
            net_income=Decimal("200"),
            cost_of_sales=Decimal("500"),
            data_state="fresh",
        )
    )

    # 5. Add InvestScreenerSnapshot
    db_session.add(
        InvestScreenerSnapshot(
            market="us",
            symbol="AAPL",
            snapshot_date=vd,
            latest_close=Decimal("290.55"),
            change_rate=Decimal("1.2"),
            daily_volume=1000,
            closes_window=[290.55],
            source="yahoo",
            computed_at=dt.datetime(2026, 6, 2, 0, 30, tzinfo=dt.UTC),
        )
    )
    await db_session.commit()

    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="us",
        spec=FUNDAMENTALS_PRESET_SPECS["profitable_company"],
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None and result.rows
    row = result.rows[0]
    assert row["close"] == pytest.approx(290.55)
    assert row["change_rate"] == pytest.approx(1.2)
    assert row["volume"] == 1000

    # Clean up again
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(InvestScreenerSnapshot).where(InvestScreenerSnapshot.symbol.in_(syms))
    )
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_us_fundamentals_rows_tolerate_missing_quote(db_session) -> None:
    """invest_screener 파티션에 해당 심볼이 없어도 행은 유지되고 키만 None."""
    await _cleanup_db(db_session)
    import sqlalchemy as sa

    from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot
    from app.models.market_valuation_snapshot import MarketValuationSnapshot
    from app.models.us_symbol_universe import USSymbolUniverse
    from app.services.invest_view_model.fundamentals_screener import (
        FUNDAMENTALS_PRESET_SPECS,
        load_fundamentals_preset_from_snapshots,
    )

    vd = dt.date(2026, 6, 2)
    syms = ["AAPL"]

    # 1. Clean up
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()

    # 2. Add US Symbol
    db_session.add(
        USSymbolUniverse(
            symbol="AAPL",
            name_en="Apple Inc.",
            name_kr="애플",
            exchange="NASDAQ",
            is_active=True,
            is_common_stock=True,
        )
    )

    # 3. Add MarketValuationSnapshot
    db_session.add(
        MarketValuationSnapshot(
            market="us",
            symbol="AAPL",
            snapshot_date=vd,
            source="yahoo",
            per=Decimal("15"),
            roe=Decimal("20"),
            market_cap=Decimal("3000000000000"),
        )
    )

    # 4. Add FinancialFundamentalsSnapshot
    db_session.add(
        FinancialFundamentalsSnapshot(
            market="us",
            symbol="AAPL",
            fiscal_period="2024A",
            period_type="annual",
            period_end_date=dt.date(2024, 12, 31),
            filing_date=dt.date(2025, 3, 20),
            effective_at=dt.date(2025, 3, 20),
            source="yfinance",
            source_collected_at=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
            revenue=Decimal("1000"),
            net_income=Decimal("200"),
            cost_of_sales=Decimal("500"),
            data_state="fresh",
        )
    )
    await db_session.commit()

    # Given: Valuation+Fundamentals만 있고 InvestScreenerSnapshot에는 행 없음
    result = await load_fundamentals_preset_from_snapshots(
        db_session,
        market="us",
        spec=FUNDAMENTALS_PRESET_SPECS["profitable_company"],
        limit=20,
        now=lambda: dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )
    assert result is not None and result.rows
    assert result.rows[0].get("close") is None  # 행 자체는 살아있음

    # Clean up again
    await db_session.execute(
        sa.delete(MarketValuationSnapshot).where(
            MarketValuationSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(FinancialFundamentalsSnapshot).where(
            FinancialFundamentalsSnapshot.symbol.in_(syms)
        )
    )
    await db_session.execute(
        sa.delete(USSymbolUniverse).where(USSymbolUniverse.symbol.in_(syms))
    )
    await db_session.commit()
