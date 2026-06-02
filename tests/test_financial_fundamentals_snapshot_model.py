from __future__ import annotations

from app.models.financial_fundamentals_snapshot import FinancialFundamentalsSnapshot


def test_table_name_and_unique_constraint():
    assert (
        FinancialFundamentalsSnapshot.__tablename__
        == "financial_fundamentals_snapshots"
    )
    constraint_names = {
        c.name for c in FinancialFundamentalsSnapshot.__table__.constraints
    }
    assert "uq_financial_fundamentals_snapshots_msfs" in constraint_names


def test_pit_and_metric_columns_present():
    cols = set(FinancialFundamentalsSnapshot.__table__.columns.keys())
    # 4 PIT time semantics kept separate (ROB-330 alignment)
    assert {
        "period_end_date",
        "filing_date",
        "effective_at",
        "source_collected_at",
    } <= cols
    # raw + discrete metric columns
    assert {
        "revenue",
        "net_income",
        "gross_profit",
        "cost_of_sales",
        "roe",
        "payout_ratio",
        "dividend_per_share",
        "discrete_revenue",
        "discrete_net_income",
        "data_state",
        "raw_payload",
        "schema_version",
        "fiscal_period",
        "period_type",
    } <= cols


def test_model_registered_for_metadata():
    # Import side-effect: appears in Base.metadata so conftest create_all builds it.
    from app.models import FinancialFundamentalsSnapshot as Exported  # noqa: F401
    from app.models.base import Base

    assert "financial_fundamentals_snapshots" in Base.metadata.tables
