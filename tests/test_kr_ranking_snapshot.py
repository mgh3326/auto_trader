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


from app.services.invest_momentum_events.query_service import RankingRow


@pytest.mark.unit
def test_ranking_row_to_screen_row_maps_fields():
    row = RankingRow(
        rank=1, symbol="005930", name="삼성전자", price=71000.0,
        change_rate=3.5, volume=12_000_000, trade_value=8.5e11, market_cap=4.2e14,
    )
    out = krs.ranking_row_to_screen_row(row)
    assert out["symbol"] == "005930"
    assert out["short_code"] == "005930"
    assert out["code"] == "005930"
    assert out["name"] == "삼성전자"
    assert out["price"] == 71000.0
    assert out["change_rate"] == 3.5
    assert out["volume"] == 12_000_000.0  # int -> float
    assert out["trade_amount"] == 8.5e11   # trade_value -> trade_amount
    assert out["market_cap"] == 4.2e14
    assert out["market"] == "kr"
    # not provided by the ranking read-model -> explicit null (no fabrication)
    assert out["per"] is None
    assert out["pbr"] is None
    assert out["dividend_yield"] is None


@pytest.mark.unit
def test_ranking_row_to_screen_row_null_safe():
    row = RankingRow(
        rank=2, symbol="000660", name=None, price=None,
        change_rate=None, volume=None, trade_value=None, market_cap=None,
    )
    out = krs.ranking_row_to_screen_row(row)
    assert out["symbol"] == "000660"
    assert out["name"] == "000660"  # falls back to symbol when name missing
    assert out["price"] is None
    assert out["volume"] is None
    assert out["trade_amount"] is None

