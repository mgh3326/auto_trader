# tests/test_momentum_ranking_query_service.py
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.invest_momentum_events.query_service import (
    MomentumRankingQueryService,
)

KST = ZoneInfo("Asia/Seoul")
NOW = dt.datetime(2026, 6, 2, 10, 0, tzinfo=KST)
TODAY = NOW.date()


def _row(rank, symbol, *, snapshot_at, trading_date, order_type="up"):
    return SimpleNamespace(
        rank=rank,
        symbol=symbol,
        name=f"name-{symbol}",
        price=Decimal("1000"),
        change_rate=Decimal("3.5"),
        volume=12345,
        trade_value=Decimal("9999"),
        market_cap=Decimal("100000"),
        order_type=order_type,
        snapshot_at=snapshot_at,
        trading_date=trading_date,
    )


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows
        self.calls = []

    async def list_momentum_events(self, *, order_type=None, limit=50, **kw):
        self.calls.append((order_type, limit))
        return [r for r in self._rows if r.order_type == order_type][:limit]


@pytest.mark.asyncio
async def test_fresh_ranking_today_recent_snapshot():
    snap = NOW - dt.timedelta(minutes=5)
    repo = _FakeRepo(
        [
            _row(1, "005930", snapshot_at=snap, trading_date=TODAY),
            _row(2, "000660", snapshot_at=snap, trading_date=TODAY),
        ]
    )
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW)
    assert out.order_type == "up"
    assert [r.symbol for r in out.rows] == ["005930", "000660"]
    assert out.rows[0].price == 1000.0  # Decimal→float
    assert out.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_stale_when_trading_date_past():
    snap = NOW - dt.timedelta(minutes=5)
    repo = _FakeRepo(
        [_row(1, "005930", snapshot_at=snap, trading_date=TODAY - dt.timedelta(days=1))]
    )
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW)
    assert out.freshness.overall == "stale"
    assert out.freshness.stale_reason == "older_trading_date"


@pytest.mark.asyncio
async def test_stale_when_snapshot_older_than_ttl():
    snap = NOW - dt.timedelta(minutes=30)  # > 15min TTL
    repo = _FakeRepo([_row(1, "005930", snapshot_at=snap, trading_date=TODAY)])
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW, ttl_minutes=15)
    assert out.freshness.overall == "stale"
    assert out.freshness.stale_reason == "older_than_ttl"


@pytest.mark.asyncio
async def test_unavailable_when_no_rows():
    repo = _FakeRepo([])
    svc = MomentumRankingQueryService(repo)
    out = await svc.get_ranking(order_type="up", limit=10, now=NOW)
    assert out.rows == ()
    assert out.freshness.overall == "unavailable"
    assert out.freshness.stale_reason == "no_ranking_rows"
