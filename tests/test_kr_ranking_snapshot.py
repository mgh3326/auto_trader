import pytest

from app.mcp_server.tooling.screening import kr_ranking_snapshot as krs
from app.services.invest_momentum_events.query_service import (
    Freshness,
    MomentumRanking,
    RankingRow,
)


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


@pytest.mark.unit
def test_ranking_row_to_screen_row_maps_fields():
    row = RankingRow(
        rank=1,
        symbol="005930",
        name="삼성전자",
        price=71000.0,
        change_rate=3.5,
        volume=12_000_000,
        trade_value=8.5e11,
        market_cap=4.2e14,
    )
    out = krs.ranking_row_to_screen_row(row)
    assert out["symbol"] == "005930"
    assert out["short_code"] == "005930"
    assert out["code"] == "005930"
    assert out["name"] == "삼성전자"
    assert out["price"] == 71000.0
    assert out["change_rate"] == 3.5
    assert out["volume"] == 12_000_000.0  # int -> float
    assert out["trade_amount"] == 8.5e11  # trade_value -> trade_amount
    assert out["market_cap"] == 4.2e14
    assert out["market"] == "kr"
    # not provided by the ranking read-model -> explicit null (no fabrication)
    assert out["per"] is None
    assert out["pbr"] is None
    assert out["dividend_yield"] is None


@pytest.mark.unit
def test_ranking_row_to_screen_row_null_safe():
    row = RankingRow(
        rank=2,
        symbol="000660",
        name=None,
        price=None,
        change_rate=None,
        volume=None,
        trade_value=None,
        market_cap=None,
    )
    out = krs.ranking_row_to_screen_row(row)
    assert out["symbol"] == "000660"
    assert out["name"] == "000660"  # falls back to symbol when name missing
    assert out["price"] is None
    assert out["volume"] is None
    assert out["trade_amount"] is None


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
    fr = Freshness(
        overall="stale", latest_snapshot_at=None, stale_reason="older_than_ttl"
    )
    data_state, meta, warnings = krs.freshness_to_meta(fr, row_count=10)
    assert data_state == "stale"
    assert meta["data_state"] == "stale"
    assert meta["stale_reason"] == "older_than_ttl"
    assert meta["retryable"] is False  # stale snapshot won't recover by immediate retry
    assert any("오래" in w for w in warnings)


class _FakeQS:
    """Fake MomentumRankingQueryService: returns canned MomentumRanking per order_type."""

    def __init__(self, by_order_type: dict[str, MomentumRanking]):
        self._by = by_order_type
        self.calls: list[str] = []

    async def get_ranking(self, *, order_type, market, limit, now, **_):
        self.calls.append(order_type)
        return self._by[order_type]


def _ranking(order_type, overall, rows):
    return MomentumRanking(
        market="kr",
        order_type=order_type,
        trading_date=None,
        rows=tuple(rows),
        freshness=Freshness(
            overall, None, None if overall == "fresh" else "older_than_ttl"
        ),
    )


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_returns_none_for_ineligible_sort():
    qs = _FakeQS({})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="rsi", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is None
    assert qs.calls == []  # never queried


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_returns_none_when_unavailable():
    qs = _FakeQS({"up": _ranking("up", "unavailable", [])})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="change_rate",
        sort_order="desc",
        limit=20,
        query_service=qs,
        enrich=False,
    )
    assert out is None  # zero rows -> live fallthrough


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_fresh_change_rate_returns_rows():
    rows = [RankingRow(1, "005930", "삼성전자", 71000.0, 3.5, 100, 5e11, 4e14)]
    qs = _FakeQS({"up": _ranking("up", "fresh", rows)})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="change_rate",
        sort_order="desc",
        limit=20,
        query_service=qs,
        enrich=False,
    )
    assert out is not None
    assert out.data_state == "fresh"
    assert out.total_count == 1
    assert out.rows[0]["symbol"] == "005930"
    assert out.source == "kr_market_ranking"
    assert qs.calls == ["up"]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_stale_returned_honestly_not_dropped():
    rows = [RankingRow(1, "005930", "삼성", 71000.0, 1.0, 100, 5e11, 4e14)]
    qs = _FakeQS({"quantTop": _ranking("quantTop", "stale", rows)})
    out = await krs.load_kr_ranking_snapshot(
        sort_by="volume", sort_order="desc", limit=20, query_service=qs, enrich=False
    )
    assert out is not None and out.rows  # stale still returns rows (no hard-0)
    assert out.data_state == "stale"
    assert any("오래" in w for w in out.warnings)


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_trade_amount_unions_buckets_and_resorts():
    up = [RankingRow(1, "A", "A", 1.0, 9.0, 10, 100.0, 5.0)]
    qt = [RankingRow(1, "B", "B", 1.0, 1.0, 99, 300.0, 1.0)]
    qs = _FakeQS(
        {
            "up": _ranking("up", "fresh", up),
            "quantTop": _ranking("quantTop", "fresh", qt),
        }
    )
    out = await krs.load_kr_ranking_snapshot(
        sort_by="trade_amount",
        sort_order="desc",
        limit=20,
        query_service=qs,
        enrich=False,
    )
    assert out is not None
    assert [r["symbol"] for r in out.rows] == ["B", "A"]  # 300 > 100
    assert set(qs.calls) == {"up", "quantTop"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_load_fail_open_on_query_error():
    class _Boom:
        async def get_ranking(self, **_):
            raise RuntimeError("db down")

    out = await krs.load_kr_ranking_snapshot(
        sort_by="volume",
        sort_order="desc",
        limit=20,
        query_service=_Boom(),
        enrich=False,
    )
    assert out is None  # fail-open -> live fallthrough


@pytest.mark.asyncio
@pytest.mark.unit
async def test_enrich_rows_fills_code_sector_valuation_best_effort():
    rows = [
        {
            "symbol": "005930",
            "code": "005930",
            "per": None,
            "pbr": None,
            "dividend_yield": None,
            "instrument_type": "stock",
            "name": "삼성전자",
        },
    ]
    universe = {
        "005930": {"code": "KR7005930003", "sector": "반도체", "name": "삼성전자"}
    }
    valuation = {"005930": {"per": 12.3, "pbr": 1.1, "dividend_yield": 2.5}}

    out = await krs._enrich_rows(
        rows,
        universe_by_code=universe,
        valuation_by_code=valuation,
    )
    assert out[0]["code"] == "KR7005930003"
    assert out[0]["sector"] == "반도체"
    assert out[0]["per"] == 12.3
    assert out[0]["pbr"] == 1.1
    assert out[0]["dividend_yield"] == 2.5


@pytest.mark.asyncio
@pytest.mark.unit
async def test_enrich_rows_no_fabrication_when_missing():
    rows = [
        {
            "symbol": "999999",
            "code": "999999",
            "per": None,
            "pbr": None,
            "dividend_yield": None,
            "instrument_type": "stock",
            "name": "x",
        }
    ]
    out = await krs._enrich_rows(rows, universe_by_code={}, valuation_by_code={})
    assert out[0]["per"] is None
    assert out[0]["pbr"] is None
    assert out[0]["dividend_yield"] is None
    assert out[0]["code"] == "999999"  # unchanged when no universe match


# --- ROB-388 follow-up: snapshot_path_applicable guard (sub-market + filter safety) ---

_GUARD_BASE: dict[str, object] = {
    "asset_type": None,
    "category": None,
    "sector": None,
    "min_market_cap": None,
    "max_per": None,
    "max_pbr": None,
    "min_dividend_yield": None,
    "min_analyst_buy": None,
    "max_rsi": None,
    "adv_krw_min": None,
    "market_cap_min_krw": None,
    "market_cap_max_krw": None,
    "instrument_types": None,
    "exclude_sectors": None,
}


@pytest.mark.unit
def test_snapshot_path_applicable_only_kr_wide():
    # KR-wide ranking is faithfully serveable
    assert krs.snapshot_path_applicable(market="kr", **_GUARD_BASE) is True
    assert krs.snapshot_path_applicable(market="all", **_GUARD_BASE) is True
    # sub-markets would mislabel KR-wide data -> not applicable
    assert krs.snapshot_path_applicable(market="kospi", **_GUARD_BASE) is False
    assert krs.snapshot_path_applicable(market="kosdaq", **_GUARD_BASE) is False
    assert krs.snapshot_path_applicable(market="konex", **_GUARD_BASE) is False


@pytest.mark.unit
def test_snapshot_path_applicable_disqualified_by_asset_or_scope():
    assert (
        krs.snapshot_path_applicable(
            market="kr", **{**_GUARD_BASE, "asset_type": "etf"}
        )
        is False
    )
    assert (
        krs.snapshot_path_applicable(
            market="kr", **{**_GUARD_BASE, "category": "반도체"}
        )
        is False
    )
    assert (
        krs.snapshot_path_applicable(market="kr", **{**_GUARD_BASE, "sector": "Tech"})
        is False
    )
    # asset_type="stock" is explicitly allowed
    assert (
        krs.snapshot_path_applicable(
            market="kr", **{**_GUARD_BASE, "asset_type": "stock"}
        )
        is True
    )


@pytest.mark.unit
def test_snapshot_path_applicable_disqualified_by_any_quality_filter():
    # the snapshot has no way to honor these -> must go live (which does)
    for key in (
        "min_market_cap",
        "max_per",
        "max_pbr",
        "min_dividend_yield",
        "min_analyst_buy",
        "max_rsi",
        "adv_krw_min",
        "market_cap_min_krw",
        "market_cap_max_krw",
    ):
        assert (
            krs.snapshot_path_applicable(market="kr", **{**_GUARD_BASE, key: 1})
            is False
        ), key
    assert (
        krs.snapshot_path_applicable(
            market="kr", **{**_GUARD_BASE, "instrument_types": ["etf"]}
        )
        is False
    )
    assert (
        krs.snapshot_path_applicable(
            market="kr", **{**_GUARD_BASE, "exclude_sectors": ["금융"]}
        )
        is False
    )
