"""WiseFn per-day fetch helper tests (ROB-171 / ROB-183)."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

# Synthetic payload mirroring the mapped row shape returned by
# _fetch_calendar_payload.  Used by tests that patch _fetch_calendar_payload.
SAMPLE_PAYLOAD = {
    "as_of_ym": "202605",
    "items": [
        {
            "stock_code": "005930",
            "corp_name": "삼성전자",
            "release_date": "2026-05-13",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": None,
            "time_hint": "after_close",
        },
        {
            "stock_code": "000660",
            "corp_name": "SK하이닉스",
            "release_date": "2026-05-13",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": None,
            "time_hint": "before_open",
        },
        {
            "stock_code": "005380",
            "corp_name": "현대차",
            "release_date": "2026-05-14",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": None,
            "time_hint": "unknown",
        },
    ],
}

# Raw WiseFn response items (will be JSON-encoded and euc-kr encoded in tests).
# Fields: jongcode, jongname, expect_dt (YYYYMMDD), gyulsan_ym (YYYYMM),
#         expect_time ("1"=before_open, "2"=after_close, "3"=during_market),
#         pub_yn ("N"=scheduled, "Y"=released), confirm_dt (YYYYMMDD if released)
_RAW_WISEFN_ITEMS = [
    {
        "jongcode": "005930",
        "jongname": "삼성전자",
        "expect_dt": "20260513",
        "confirm_dt": "",
        "gyulsan_ym": "202603",
        "expect_time": "2",
        "pub_yn": "N",
    },
    {
        "jongcode": "000660",
        "jongname": "SK하이닉스",
        "expect_dt": "20260513",
        "confirm_dt": "",
        "gyulsan_ym": "202603",
        "expect_time": "1",
        "pub_yn": "N",
    },
    {
        "jongcode": "005380",
        "jongname": "현대차",
        "expect_dt": "20260514",
        "confirm_dt": "",
        "gyulsan_ym": "202603",
        "expect_time": "3",
        "pub_yn": "N",
    },
]


def _to_euc_kr_bytes(data: object) -> bytes:
    """Encode data as JSON then as euc-kr bytes (mirrors WiseFn wire format)."""
    return json.dumps(data, ensure_ascii=False).encode("euc-kr")


# ---------------------------------------------------------------------------
# HTTP seam tests (no live WiseFn call)
# ---------------------------------------------------------------------------


def test_wisefn_http_base_url_uses_canonical_www_host():
    """WiseFn redirects the apex host; use canonical www to avoid 301 retry loops."""
    from app.services.market_events import wisefn_helpers as wf

    assert wf._WISEFN_BASE_URL == "https://www.wisereport.co.kr"


def test_wisefn_http_calendar_path_uses_live_wisecalendar_endpoint():
    """Live WiseFn calendar JSON is served under /wiseCalendar/ in ROB-183 smoke."""
    from app.services.market_events import wisefn_helpers as wf

    assert wf._WISEFN_CALENDAR_PATH == "/wiseCalendar/GetCalendarAjax.aspx"


# ---------------------------------------------------------------------------
# Public coroutine tests (patch _fetch_calendar_payload)
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# _fetch_calendar_payload HTTP-level tests (patch _http_get_monthly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_decodes_euc_kr_and_maps_fields():
    """Happy path: euc-kr JSON bytes → normalized items."""
    from app.services.market_events import wisefn_helpers as wf

    raw_bytes = _to_euc_kr_bytes(_RAW_WISEFN_ITEMS)

    with patch.object(wf, "_http_get_monthly", AsyncMock(return_value=raw_bytes)):
        payload = await wf._fetch_calendar_payload(date(2026, 5, 13))

    assert payload["as_of_ym"] == "202605"
    items = payload["items"]
    assert len(items) == 3

    samsung = next(i for i in items if i["stock_code"] == "005930")
    assert samsung["release_date"] == "2026-05-13"
    assert samsung["fiscal_year"] == 2026
    assert samsung["fiscal_quarter"] == 1
    assert samsung["time_hint"] == "after_close"
    assert samsung["release_type"] == "scheduled"
    assert samsung["corp_name"] == "삼성전자"

    sk = next(i for i in items if i["stock_code"] == "000660")
    assert sk["time_hint"] == "before_open"

    hyundai = next(i for i in items if i["stock_code"] == "005380")
    assert hyundai["release_date"] == "2026-05-14"
    assert hyundai["time_hint"] == "during_market"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_maps_current_wisecalendar_fields():
    """Current live /wiseCalendar fields map to normalized earnings rows."""
    from app.services.market_events import wisefn_helpers as wf

    current_item = [
        {
            "CMP_CD": "A005300",
            "CMP_NM_KOR": "롯데칠성 (연결)",
            "DAY_DT": "04",
            "WK_DT": "2026-05-04 (월)",
            "TERM_TYP": 32,
            "VAL05": "2026/03(분기)",
        }
    ]
    raw_bytes = _to_euc_kr_bytes(current_item)

    with patch.object(wf, "_http_get_monthly", AsyncMock(return_value=raw_bytes)):
        payload = await wf._fetch_calendar_payload(date(2026, 5, 4))

    assert payload["as_of_ym"] == "202605"
    assert payload["items"] == [
        {
            "stock_code": "005300",
            "corp_name": "롯데칠성",
            "release_date": "2026-05-04",
            "fiscal_year": 2026,
            "fiscal_quarter": 1,
            "release_type": "scheduled",
            "title": None,
            "time_hint": "unknown",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_released_row_uses_confirm_dt():
    """When pub_yn='Y' and confirm_dt is set, the confirmed date is used."""
    from app.services.market_events import wisefn_helpers as wf

    released_item = [
        {
            "jongcode": "005930",
            "jongname": "삼성전자",
            "expect_dt": "20260510",
            "confirm_dt": "20260513",
            "gyulsan_ym": "202603",
            "expect_time": "2",
            "pub_yn": "Y",
        }
    ]
    raw_bytes = _to_euc_kr_bytes(released_item)

    with patch.object(wf, "_http_get_monthly", AsyncMock(return_value=raw_bytes)):
        payload = await wf._fetch_calendar_payload(date(2026, 5, 13))

    items = payload["items"]
    assert len(items) == 1
    assert items[0]["release_date"] == "2026-05-13"
    assert items[0]["release_type"] == "released"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_retries_and_raises_after_all_failures(
    monkeypatch,
):
    """All retries exhausted → RuntimeError is raised."""
    import httpx as _httpx

    from app.services.market_events import wisefn_helpers as wf

    monkeypatch.setattr(wf, "_WISEFN_BACKOFF_BASE_SEC", 0.0)
    monkeypatch.setattr(wf, "_WISEFN_MAX_RETRIES", 2)

    with patch.object(
        wf,
        "_http_get_monthly",
        AsyncMock(side_effect=_httpx.RequestError("connection refused")),
    ):
        with pytest.raises(RuntimeError, match="all.*attempts failed"):
            await wf._fetch_calendar_payload(date(2026, 5, 13))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_malformed_json_raises_after_retries(
    monkeypatch,
):
    """Malformed (non-JSON) bytes → RuntimeError after all retries."""
    from app.services.market_events import wisefn_helpers as wf

    monkeypatch.setattr(wf, "_WISEFN_BACKOFF_BASE_SEC", 0.0)
    monkeypatch.setattr(wf, "_WISEFN_MAX_RETRIES", 2)

    with patch.object(
        wf,
        "_http_get_monthly",
        AsyncMock(return_value=b"not-valid-json"),
    ):
        with pytest.raises(RuntimeError, match="all.*attempts failed"):
            await wf._fetch_calendar_payload(date(2026, 5, 13))


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_item_without_date_is_skipped():
    """Items with no usable date are silently dropped."""
    from app.services.market_events import wisefn_helpers as wf

    mixed = [
        {
            "jongcode": "005930",
            "jongname": "삼성전자",
            "expect_dt": "",  # no date → skip
            "confirm_dt": "",
            "gyulsan_ym": "202603",
            "expect_time": "2",
            "pub_yn": "N",
        },
        {
            "jongcode": "000660",
            "jongname": "SK하이닉스",
            "expect_dt": "20260513",
            "confirm_dt": "",
            "gyulsan_ym": "202603",
            "expect_time": "1",
            "pub_yn": "N",
        },
    ]
    raw_bytes = _to_euc_kr_bytes(mixed)

    with patch.object(wf, "_http_get_monthly", AsyncMock(return_value=raw_bytes)):
        payload = await wf._fetch_calendar_payload(date(2026, 5, 13))

    assert len(payload["items"]) == 1
    assert payload["items"][0]["stock_code"] == "000660"


@pytest.mark.asyncio
@pytest.mark.unit
async def test_fetch_calendar_payload_empty_array_returns_empty_items():
    """Empty JSON array response → empty items list without error."""
    from app.services.market_events import wisefn_helpers as wf

    raw_bytes = _to_euc_kr_bytes([])

    with patch.object(wf, "_http_get_monthly", AsyncMock(return_value=raw_bytes)):
        payload = await wf._fetch_calendar_payload(date(2026, 5, 13))

    assert payload["items"] == []
    assert payload["as_of_ym"] == "202605"
