"""Unit tests for the scraping-backed DART daily filing helper (ROB-1071)."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.market_events import dart_helpers
from app.services.market_events.normalizers import normalize_dart_disclosure_row


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_uses_list_date_ex_and_returns_json_safe_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        [
            {
                "rcept_dt": pd.Timestamp("2026-07-24 07:31:00"),
                "corp_cls": "Y",
                "corp_name": "테스트",
                "rcept_no": "20260724000001",
                "report_nm": "주요사항보고서",
                "flr_nm": "테스트",
                "rm": "",
            }
        ]
    )
    client = MagicMock()
    client.list_date_ex.return_value = frame
    monkeypatch.setattr(dart_helpers, "_get_client", AsyncMock(return_value=client))

    rows = await dart_helpers.fetch_dart_filings_for_date(date(2026, 7, 24))

    client.list_date_ex.assert_called_once_with("2026-07-24")
    assert rows == [
        {
            "rcept_dt": "2026-07-24T07:31:00",
            "corp_cls": "Y",
            "corp_name": "테스트",
            "rcept_no": "20260724000001",
            "report_nm": "주요사항보고서",
            "flr_nm": "테스트",
            "rm": "",
        }
    ]
    event, _ = normalize_dart_disclosure_row(rows[0])
    assert event["event_date"] == date(2026, 7, 24)
    json.dumps(event["raw_payload_json"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_fails_closed_when_list_date_ex_column_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    frame = pd.DataFrame(
        columns=sorted(dart_helpers.DART_LIST_DATE_EX_REQUIRED_COLUMNS - {"rcept_no"})
    )
    client = MagicMock()
    client.list_date_ex.return_value = frame
    monkeypatch.setattr(dart_helpers, "_get_client", AsyncMock(return_value=client))

    with pytest.raises(
        dart_helpers.DartResponseSchemaError,
        match=r"missing required columns: rcept_no",
    ):
        await dart_helpers.fetch_dart_filings_for_date(date(2026, 7, 24))
