"""WiseFn per-day fetch helper tests (ROB-171, fixture-only)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

# Synthetic minimal payload mirroring the row shape we plan to consume.
SAMPLE_PAYLOAD = {
    "as_of_date": "2026-05-13",
    "items": [
        {
            "stock_code": "005930",
            "corp_name": "삼성전자",
            "release_date": "2026-05-13",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": "삼성전자 2026년 1분기 실적발표 예정",
            "time_hint": "after_close",
        },
        {
            "stock_code": "000660",
            "corp_name": "SK하이닉스",
            "release_date": "2026-05-13",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": "SK하이닉스 2026년 1분기 실적발표 예정",
            "time_hint": "before_open",
        },
        {
            "stock_code": "005380",
            "corp_name": "현대차",
            "release_date": "2026-05-14",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": "현대차 2026년 1분기 실적발표 예정",
            "time_hint": "unknown",
        },
    ],
}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_wisefn_for_date_filters_to_target_day():
    from app.services.market_events import wisefn_helpers as wf

    with patch.object(
        wf,
        "_fetch_calendar_payload",
        AsyncMock(return_value=SAMPLE_PAYLOAD),
    ):
        rows = await wf.fetch_wisefn_earnings_for_date(date(2026, 5, 13))

    assert len(rows) == 2
    assert {r["stock_code"] for r in rows} == {"005930", "000660"}


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_wisefn_for_date_returns_empty_list_when_no_match():
    from app.services.market_events import wisefn_helpers as wf

    with patch.object(
        wf,
        "_fetch_calendar_payload",
        AsyncMock(return_value=SAMPLE_PAYLOAD),
    ):
        rows = await wf.fetch_wisefn_earnings_for_date(date(2026, 6, 1))

    assert rows == []


@pytest.mark.asyncio
@pytest.mark.unit
async def test_default_fetch_calendar_payload_raises_not_implemented():
    """The live fetch is intentionally disabled until upstream contract is confirmed."""
    from app.services.market_events import wisefn_helpers as wf

    with pytest.raises(NotImplementedError):
        await wf._fetch_calendar_payload(date(2026, 5, 13))
