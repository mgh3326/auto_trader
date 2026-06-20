import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.list_recent_watch_events import collect, main
from tests._watch_events_helpers import mk_watch_event, utc_at


@pytest.mark.asyncio
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
    assert set(ev) == {
        "event_uuid", "symbol", "market", "source_report_uuid",
        "metric", "operator", "threshold", "current_value",
        "delivered_at", "kst_date",
    }


def test_main_bad_since_emits_error_json_and_nonzero(capsys):
    rc = main(["--since", "not-a-date", "--market", "crypto"])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is False
    assert "error" in payload
