import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.investment_reports.repository import InvestmentReportsRepository
from tests._watch_events_helpers import mk_watch_event, utc_at

pytestmark = pytest.mark.asyncio


async def test_returns_only_delivered_after_since_ordered_asc(session: AsyncSession):
    await mk_watch_event(session, symbol="KRW-AAA", delivered_at=utc_at(0))
    e1 = await mk_watch_event(session, symbol="KRW-BBB", delivered_at=utc_at(10))
    e2 = await mk_watch_event(session, symbol="KRW-CCC", delivered_at=utc_at(20))
    await mk_watch_event(session, symbol="KRW-DDD", delivery_status="pending")
    await session.commit()

    repo = InvestmentReportsRepository(session)
    rows = await repo.list_events_by_delivery_status(
        delivery_status="delivered", delivered_since=utc_at(5), market="crypto", limit=50
    )

    # TRUNCATE 격리라 정확 리스트 단언 안전: delivered + >=since + asc + pending 제외
    assert [r.symbol for r in rows] == ["KRW-BBB", "KRW-CCC"]
    assert {r.event_uuid for r in rows} == {e1.event_uuid, e2.event_uuid}


async def test_market_filter_and_limit_clamp(session: AsyncSession):
    await mk_watch_event(session, symbol="005930", market="kr", delivered_at=utc_at(0))
    await mk_watch_event(session, symbol="KRW-EEE", market="crypto", delivered_at=utc_at(0))
    await session.commit()

    repo = InvestmentReportsRepository(session)
    kr = await repo.list_events_by_delivery_status(market="kr", limit=0)  # clamp -> >=1
    assert [r.symbol for r in kr] == ["005930"]
    assert all(r.market == "kr" for r in kr)
