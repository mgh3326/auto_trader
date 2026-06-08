import pytest

from app.mcp_server.tooling.screening import kr_ranking_snapshot as krs


@pytest.mark.unit
def test_snapshot_eligible_sorts_membership():
    assert krs.is_snapshot_eligible_sort("change_rate") is True
    assert krs.is_snapshot_eligible_sort("volume") is True
    assert krs.is_snapshot_eligible_sort("trade_amount") is True
    assert krs.is_snapshot_eligible_sort("market_cap") is True
    # not covered by the momentum ranking read-model -> must go live
    assert krs.is_snapshot_eligible_sort("dividend_yield") is False
    assert krs.is_snapshot_eligible_sort("week_change_rate") is False
    assert krs.is_snapshot_eligible_sort("rsi") is False
    assert krs.is_snapshot_eligible_sort("score") is False


@pytest.mark.unit
def test_order_types_for_sort():
    # direct single-bucket dimensions
    assert krs.order_types_for_sort("change_rate") == ("up",)
    assert krs.order_types_for_sort("volume") == ("quantTop",)
    # re-sort dimensions union both default buckets
    assert krs.order_types_for_sort("trade_amount") == ("up", "quantTop")
    assert krs.order_types_for_sort("market_cap") == ("up", "quantTop")
    # ineligible -> empty
    assert krs.order_types_for_sort("rsi") == ()
