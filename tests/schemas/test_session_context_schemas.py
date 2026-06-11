from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.schemas.session_context import (
    SessionContextAppendEntry,
    SessionContextRecentRequest,
    SessionContextRefs,
    SessionContextResponse,
)


def test_append_entry_accepts_refs_and_strips_title_body() -> None:
    entry = SessionContextAppendEntry.model_validate(
        {
            "kst_date": "2026-06-11",
            "market": "kr",
            "account_scope": "kis_live",
            "entry_type": "deferred",
            "title": "  DB 매도 보류  ",
            "body": "  익절 조건만 허용되어 매도 제외  ",
            "refs": {
                "report_uuid": "11111111-1111-1111-1111-111111111111",
                "item_uuid": "22222222-2222-2222-2222-222222222222",
                "alert_uuid": "33333333-3333-3333-3333-333333333333",
                "order_id": "KIS-1",
                "journal_id": 7,
                "symbols": ["  DB  ", "005930", ""],
            },
            "created_by": "claude",
            "session_label": "kr-2026-06-11-close",
        }
    )

    assert entry.kst_date == date(2026, 6, 11)
    assert entry.title == "DB 매도 보류"
    assert entry.body == "익절 조건만 허용되어 매도 제외"
    assert entry.refs.report_uuid == UUID("11111111-1111-1111-1111-111111111111")
    assert entry.refs.symbols == ["DB", "005930"]


def test_append_entry_rejects_unknown_type_and_extra_ref() -> None:
    with pytest.raises(ValidationError) as exc_info:
        SessionContextAppendEntry.model_validate(
            {
                "market": "kr",
                "entry_type": "memo",
                "title": "x",
                "body": "y",
                "refs": {"unknown": "value"},
            }
        )

    rendered = str(exc_info.value)
    assert "entry_type" in rendered
    assert "unknown" in rendered


def test_recent_request_clamps_limit_and_parses_date() -> None:
    request = SessionContextRecentRequest.model_validate(
        {
            "market": "kr",
            "account_scope": "kis_mock",
            "entry_type": "next_action",
            "kst_date_from": "2026-06-10",
            "limit": 500,
        }
    )

    assert request.limit == 100
    assert request.kst_date_from == date(2026, 6, 10)


def test_response_serializes_refs_from_attributes() -> None:
    class Row:
        entry_uuid = UUID("44444444-4444-4444-4444-444444444444")
        kst_date = date(2026, 6, 11)
        market = "kr"
        account_scope = "kis_live"
        entry_type = "handoff_note"
        title = "handoff"
        body = "continue tournament"
        refs = {"symbols": ["005930"]}
        created_by = "operator"
        session_label = None
        created_at = datetime(2026, 6, 11, 1, 2, 3, tzinfo=UTC)

    response = SessionContextResponse.model_validate(Row())

    assert response.refs == SessionContextRefs(symbols=["005930"])
    dumped = response.model_dump(mode="json")
    assert dumped["entry_uuid"] == "44444444-4444-4444-4444-444444444444"
    assert dumped["refs"]["symbols"] == ["005930"]
