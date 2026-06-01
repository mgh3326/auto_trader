import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from app.services.action_report.snapshot_backed.collectors.investor_flow import (
    InvestorFlowSnapshotCollector,
)
from app.services.investment_snapshots.collectors import CollectorRequest
from app.services.investor_flow_snapshots.query_service import (
    Freshness,
    InvestorFlow,
    InvestorFlowRow,
)

KST = ZoneInfo("Asia/Seoul")


def _request(symbols=("005930",)):
    return CollectorRequest(
        market="kr", account_scope=None, symbols=list(symbols), policy_snapshot={}
    )


def _flow(overall="fresh"):
    return InvestorFlow(
        market="kr",
        snapshot_date=dt.date(2026, 6, 2),
        rows=(
            InvestorFlowRow(
                "005930", 100, 50, -150, True, False, 3, 0, 2, 0
            ),
        ),
        freshness=Freshness(overall, dt.date(2026, 6, 2), None, 0),
    )


class _FakeQuery:
    def __init__(self, *, flow=None, raises=False):
        self._flow = flow
        self._raises = raises

    async def get_investor_flow(self, *, symbols, market="kr", now, ttl_days=1):
        if self._raises:
            raise RuntimeError("boom")
        return self._flow


@pytest.mark.asyncio
async def test_collect_returns_investor_flow_payload():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=_flow("fresh")))
    assert collector.snapshot_kind == "investor_flow"
    results = await collector.collect(_request())
    assert len(results) == 1
    r = results[0]
    assert r.snapshot_kind == "investor_flow"
    assert r.payload_json["rows"][0]["symbol"] == "005930"
    assert r.payload_json["rows"][0]["double_buy"] is True
    assert r.freshness_status == "fresh"


@pytest.mark.asyncio
async def test_collect_stale_maps_to_soft_stale():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=_flow("stale")))
    results = await collector.collect(_request())
    assert results[0].freshness_status == "soft_stale"


@pytest.mark.asyncio
async def test_collect_unavailable_when_no_rows():
    empty = InvestorFlow("kr", None, (), Freshness("unavailable", None, "no_flow_rows", None))
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=empty))
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"


@pytest.mark.asyncio
async def test_collect_degrades_on_error():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(raises=True))
    results = await collector.collect(_request())
    assert results[0].freshness_status == "unavailable"
    assert "reason" in results[0].errors_json


@pytest.mark.asyncio
async def test_collect_unavailable_when_no_symbols():
    collector = InvestorFlowSnapshotCollector(session=None, query_service=_FakeQuery(flow=_flow()))
    results = await collector.collect(_request(symbols=()))
    assert results[0].freshness_status == "unavailable"
