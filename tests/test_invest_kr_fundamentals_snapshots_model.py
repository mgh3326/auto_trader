from __future__ import annotations

import sqlalchemy as sa

from app.models.invest_kr_fundamentals_snapshot import InvestKrFundamentalsSnapshot


def _constraint_sql(name: str) -> str:
    table = InvestKrFundamentalsSnapshot.__table__
    constraint = next(c for c in table.constraints if c.name == name)
    assert isinstance(constraint, sa.CheckConstraint)
    return str(constraint.sqltext)


def test_kr_fundamentals_table_name_and_unique_constraint() -> None:
    table = InvestKrFundamentalsSnapshot.__table__
    assert table.name == "invest_kr_fundamentals_snapshots"
    assert "uq_invest_kr_fundamentals_snapshots_symbol_date" in {
        c.name for c in table.constraints
    }


def test_kr_fundamentals_source_check_constraint() -> None:
    assert "tvscreener_kr" in _constraint_sql(
        "ck_invest_kr_fundamentals_snapshots_source"
    )


def test_kr_fundamentals_has_fundamentals_columns() -> None:
    columns = InvestKrFundamentalsSnapshot.__table__.columns
    for name in (
        "symbol",
        "snapshot_date",
        "name",
        "price",
        "change_rate",
        "volume",
        "market_cap",
        "per",
        "pbr",
        "dividend_yield",
        "roe_ttm",
        "payout_ratio_ttm",
        "gross_margin_ttm",
        "revenue_yoy",
        "eps_yoy",
        "eps_qoq",
        "net_income_yoy",
        "net_income_cagr_5y",
        "continuous_dividend_payout",
        "continuous_dividend_growth",
        "week_high_52",
        "rsi14",
        "sector",
        "industry",
        "raw_payload",
        "source",
        "computed_at",
    ):
        assert name in columns


def test_kr_fundamentals_required_columns_not_nullable() -> None:
    columns = InvestKrFundamentalsSnapshot.__table__.columns
    assert columns["symbol"].nullable is False
    assert columns["snapshot_date"].nullable is False
    assert columns["source"].nullable is False
    # Numeric fundamentals columns are sparse — all nullable.
    assert columns["price"].nullable is True
    assert columns["roe_ttm"].nullable is True
    assert columns["name"].nullable is True
