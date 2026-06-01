import datetime as dt
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app.services.investor_flow_snapshots.query_service import InvestorFlowQueryService

KST = ZoneInfo("Asia/Seoul")
NOW = dt.datetime(2026, 6, 2, 18, 0, tzinfo=KST)
TODAY = NOW.date()


def _row(symbol, *, snapshot_date, foreign_net=100, institution_net=50, double_buy=True):
    return SimpleNamespace(
        symbol=symbol,
        snapshot_date=snapshot_date,
        foreign_net=foreign_net,
        institution_net=institution_net,
        individual_net=-150,
        double_buy=double_buy,
        double_sell=False,
        foreign_consecutive_buy_days=3,
        foreign_consecutive_sell_days=0,
        institution_consecutive_buy_days=2,
        institution_consecutive_sell_days=0,
    )


class _FakeRepo:
    def __init__(self, rows):
        self._rows = rows

    async def latest_by_symbols(self, *, market, symbols, as_of=None):
        wanted = {s.upper() for s in symbols}
        return [r for r in self._rows if r.symbol.upper() in wanted]


@pytest.mark.asyncio
async def test_fresh_today():
    repo = _FakeRepo([_row("005930", snapshot_date=TODAY)])
    svc = InvestorFlowQueryService(repo)
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW, ttl_days=1)
    assert out.rows[0].symbol == "005930"
    assert out.rows[0].double_buy is True
    assert out.rows[0].foreign_consecutive_buy_days == 3
    assert out.freshness.overall == "fresh"


@pytest.mark.asyncio
async def test_yesterday_still_fresh_with_ttl_1():
    repo = _FakeRepo([_row("005930", snapshot_date=TODAY - dt.timedelta(days=1))])
    svc = InvestorFlowQueryService(repo)
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW, ttl_days=1)
    assert out.freshness.overall == "fresh"
    assert out.freshness.age_days == 1


@pytest.mark.asyncio
async def test_old_is_stale():
    repo = _FakeRepo([_row("005930", snapshot_date=TODAY - dt.timedelta(days=3))])
    svc = InvestorFlowQueryService(repo)
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW, ttl_days=1)
    assert out.freshness.overall == "stale"
    assert out.freshness.stale_reason == "older_than_ttl"
    assert out.freshness.age_days == 3


@pytest.mark.asyncio
async def test_no_rows_unavailable():
    svc = InvestorFlowQueryService(_FakeRepo([]))
    out = await svc.get_investor_flow(symbols=["005930"], now=NOW)
    assert out.rows == ()
    assert out.freshness.overall == "unavailable"
    assert out.freshness.stale_reason == "no_flow_rows"
