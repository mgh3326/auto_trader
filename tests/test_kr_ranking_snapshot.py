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


from app.services.invest_momentum_events.query_service import Freshness


@pytest.mark.unit
def test_dedupe_and_sort_rows_by_trade_amount_desc():
    rows = [
        {"symbol": "A", "trade_amount": 100.0, "market_cap": 5.0},
        {"symbol": "B", "trade_amount": 300.0, "market_cap": 1.0},
        {"symbol": "A", "trade_amount": 100.0, "market_cap": 5.0},  # dup symbol
    ]
    out = krs.dedupe_and_sort_rows(rows, sort_by="trade_amount", sort_order="desc")
    assert [r["symbol"] for r in out] == ["B", "A"]  # deduped + sorted desc


@pytest.mark.unit
def test_dedupe_and_sort_rows_market_cap_asc_nulls_last():
    rows = [
        {"symbol": "A", "market_cap": None},
        {"symbol": "B", "market_cap": 2.0},
        {"symbol": "C", "market_cap": 1.0},
    ]
    out = krs.dedupe_and_sort_rows(rows, sort_by="market_cap", sort_order="asc")
    assert [r["symbol"] for r in out] == ["C", "B", "A"]  # None sorts last


@pytest.mark.unit
def test_freshness_to_meta_fresh():
    fr = Freshness(overall="fresh", latest_snapshot_at=None, stale_reason=None)
    data_state, meta, warnings = krs.freshness_to_meta(fr, row_count=20)
    assert data_state == "fresh"
    assert meta["source"] == "kr_market_ranking"
    assert meta["data_state"] == "fresh"
    # coverage caveat is always present (top-movers, not full universe)
    assert any("전체 KRX 스캔" in w for w in warnings)


@pytest.mark.unit
def test_freshness_to_meta_stale_adds_warning_and_not_retryable():
    fr = Freshness(overall="stale", latest_snapshot_at=None, stale_reason="older_than_ttl")
    data_state, meta, warnings = krs.freshness_to_meta(fr, row_count=10)
    assert data_state == "stale"
    assert meta["data_state"] == "stale"
    assert meta["stale_reason"] == "older_than_ttl"
    assert meta["retryable"] is False  # stale snapshot won't recover by immediate retry
    assert any("오래" in w for w in warnings)


