import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from scripts.list_recent_watch_events import _source_report_link_state, collect, main
from tests._watch_events_helpers import mk_watch_event, utc_at


@pytest_asyncio.fixture(name="session")
async def _committed_session(
    committed_investment_reports_session: AsyncSession,
) -> AsyncIterator[AsyncSession]:
    """Expose committed rows to the CLI's independent database session."""
    yield committed_investment_reports_session


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
        "event_uuid",
        "symbol",
        "market",
        "source_report_uuid",
        "source_report_link_state",
        "metric",
        "operator",
        "threshold",
        "current_value",
        "delivered_at",
        "kst_date",
    }
    assert ev["source_report_link_state"] == "not_applicable_direct_watch"


def test_main_bad_since_emits_error_json_and_nonzero(capsys):
    rc = main(["--since", "not-a-date", "--market", "crypto"])
    assert rc == 1
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["success"] is False
    assert "error" in payload


def test_uuid5_source_is_classified_as_legacy_placeholder() -> None:
    source_uuid = uuid.uuid5(uuid.NAMESPACE_URL, "legacy-direct-watch")
    assert _source_report_link_state(source_uuid) == "legacy_direct_watch_placeholder"
