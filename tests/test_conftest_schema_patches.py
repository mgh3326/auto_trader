import pytest

from app.models.investment_snapshots import InvestmentSnapshot
from app.models.market_valuation_snapshot import MarketValuationSnapshot
from tests import conftest


@pytest.mark.unit
def test_market_valuation_source_constraint_is_current_when_all_values_present():
    definition = (
        "CHECK ((source = ANY (ARRAY['naver_finance'::text, "
        "'yahoo'::text, 'toss_openapi'::text])))"
    )

    assert not conftest._constraint_definitions_need_refresh(
        [definition], conftest.MARKET_VALUATION_SOURCE_VALUES
    )


@pytest.mark.unit
def test_market_valuation_source_constraint_refreshes_when_toss_source_missing():
    definition = "CHECK ((source = ANY (ARRAY['naver_finance'::text, 'yahoo'::text])))"

    assert conftest._constraint_definitions_need_refresh(
        [definition], conftest.MARKET_VALUATION_SOURCE_VALUES
    )


@pytest.mark.unit
def test_snapshot_kind_patch_values_match_model_constraint():
    constraint = next(
        c
        for c in InvestmentSnapshot.__table__.constraints
        if getattr(c, "name", "") == conftest.SNAPSHOT_KIND_MODEL_CHECK_NAME
    )
    model_sql = str(constraint.sqltext)

    for value in conftest.SNAPSHOT_KIND_VALUES:
        assert value in model_sql
    assert "kr_market_ranking" in conftest.SNAPSHOT_KIND_VALUES
    assert "investor_flow" in conftest.SNAPSHOT_KIND_VALUES


@pytest.mark.unit
def test_market_valuation_patch_values_match_model_constraint():
    constraint = next(
        c
        for c in MarketValuationSnapshot.__table_args__
        if getattr(c, "name", "") == conftest.MARKET_VALUATION_SOURCE_MODEL_CHECK_NAME
    )
    model_sql = str(constraint.sqltext)

    for value in conftest.MARKET_VALUATION_SOURCE_VALUES:
        assert value in model_sql
