# tests/test_kr_market_ranking_collector.py
import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.services.action_report.snapshot_backed.collectors.kr_market_ranking import (
    KrMarketRankingSnapshotCollector,
)
from app.services.invest_momentum_events.query_service import (
    Freshness,
    MomentumRanking,
    RankingRow,
)
from app.services.investment_snapshots.collectors import CollectorRequest

KST = ZoneInfo("Asia/Seoul")


def _request():
    return CollectorRequest(market="kr", account_scope=None, policy_snapshot={})


class _FakeQuery:
    def __init__(self, *, by_order, raises=False):
        self._by_order = by_order
        self._raises = raises

    async def get_ranking(
        self, *, order_type, market="kr", limit=50, now, ttl_minutes=15
    ):
        if self._raises:
            raise RuntimeError("query failed")
        return self._by_order[order_type]


def _fresh_ranking(order_type):
    return MomentumRanking(
        market="kr",
        order_type=order_type,
        trading_date=dt.date(2026, 6, 2),
        rows=(RankingRow(1, "005930", "삼성전자", 1000.0, 3.5, 100, 9999.0, 1e5),),
        freshness=Freshness("fresh", dt.datetime(2026, 6, 2, 10, tzinfo=KST), None),
    )


@pytest.mark.asyncio
async def test_collect_returns_kr_market_ranking_payload():
    query = _FakeQuery(
        by_order={"up": _fresh_ranking("up"), "quantTop": _fresh_ranking("quantTop")}
    )
    collector = KrMarketRankingSnapshotCollector(session=None, query_service=query)
    assert collector.snapshot_kind == "kr_market_ranking"
    results = await collector.collect(_request())
    assert len(results) == 1
    r = results[0]
    assert r.snapshot_kind == "kr_market_ranking"
    assert set(r.payload_json["order_types"]) == {"up", "quantTop"}
    assert r.payload_json["order_types"]["up"]["rows"][0]["symbol"] == "005930"
    assert r.freshness_status == "fresh"


@pytest.mark.asyncio
async def test_collect_unavailable_when_all_unavailable():
    empty = MomentumRanking(
        "kr", "up", None, (), Freshness("unavailable", None, "no_ranking_rows")
    )
    query = _FakeQuery(by_order={"up": empty, "quantTop": empty})
    collector = KrMarketRankingSnapshotCollector(session=None, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_collect_degrades_on_query_error():
    query = _FakeQuery(by_order={}, raises=True)
    collector = KrMarketRankingSnapshotCollector(session=None, query_service=query)
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "reason" in results[0].errors_json
