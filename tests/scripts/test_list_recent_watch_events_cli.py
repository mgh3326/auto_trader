import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.list_recent_watch_events import collect
from tests._watch_events_helpers import mk_watch_event, utc_at

pytestmark = pytest.mark.asyncio


async def test_collect_returns_serializable_delivered_events(session: AsyncSession):
    await mk_watch_event(session, symbol="KRW-XYZ", delivered_at=utc_at(0))
    await session.commit()

    out = await collect(market="crypto", since=None, limit=50)
    assert out["success"] is True
    assert out["count"] >= 1
    # JSON 직렬화 가능 (bash가 jq로 파싱)
    blob = json.dumps(out)
    assert "KRW-XYZ" in blob
    ev = next(e for e in out["events"] if e["symbol"] == "KRW-XYZ")
    assert set(ev) >= {"event_uuid", "symbol", "market", "source_report_uuid", "delivered_at"}
