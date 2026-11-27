"""Market-wide DART daily list on the official OpenDART API (hk 6408).

Pure unit tests: the closed-error client's transport is replaced, no request
leaves the process, and no database is touched. The list fixture is shaped from
the official field table (synthetic values, see the fixture's ``_note``).
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.timezone import KST
from app.services.market_events import dart_capital, dart_helpers
from app.services.market_events.dart_capital import DartDetailError
from app.services.market_events.normalizers import normalize_dart_disclosure_row

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "dart_official"
    / "list_json_doc_shape.json"
)
DAY = date(2026, 11, 27)


def _fixture_payload() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


# -- paging through list.json ---------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_pages_the_whole_receipt_day_with_day_bounds(monkeypatch):
    first = {"list": [{"rcept_no": "1", "rcept_dt": "20261127"}], "total_page": 2}
    second = {"list": [{"rcept_no": "2", "rcept_dt": "20261127"}], "total_page": 2}
    calls: list[dict] = []

    async def fake_page(**params):
        calls.append(params)
        return first if params["page_no"] == "1" else second

    monkeypatch.setattr(dart_capital, "request_list_page", fake_page)

    rows = await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert [row["rcept_no"] for row in rows] == ["1", "2"]
    assert calls == [
        {
            "bgn_de": "20261127",
            "end_de": "20261127",
            "page_no": "1",
            "page_count": "100",
        },
        {
            "bgn_de": "20261127",
            "end_de": "20261127",
            "page_no": "2",
            "page_count": "100",
        },
    ]


@pytest.mark.asyncio
async def test_fetch_no_data_day_returns_empty_list(monkeypatch):
    monkeypatch.setattr(dart_capital, "request_list_page", AsyncMock(return_value=None))

    assert await dart_helpers.fetch_dart_filings_for_date(DAY) == []


@pytest.mark.asyncio
async def test_fetch_deduplicates_a_receipt_that_shifts_between_pages(monkeypatch):
    pages = {
        "1": {"list": [{"rcept_no": "1"}, {"rcept_no": "2"}], "total_page": 2},
        "2": {"list": [{"rcept_no": "2"}, {"rcept_no": "3"}], "total_page": 2},
    }

    async def fake_page(**params):
        return pages[params["page_no"]]

    monkeypatch.setattr(dart_capital, "request_list_page", fake_page)

    rows = await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert [row["rcept_no"] for row in rows] == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_fetch_refuses_a_list_longer_than_the_page_limit(monkeypatch):
    async def endless(**params):
        return {"list": [{"rcept_no": params["page_no"]}], "total_page": 999}

    monkeypatch.setattr(dart_capital, "request_list_page", endless)

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert raised.value.failure_code == "bad_response"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"total_page": 1},  # no list
        {"list": "not-a-list", "total_page": 1},
        {"list": [1, 2], "total_page": 1},  # rows that are not objects
        {"list": [{"rcept_no": "1"}]},  # no page count
    ],
)
async def test_fetch_refuses_a_malformed_page(monkeypatch, payload):
    monkeypatch.setattr(
        dart_capital, "request_list_page", AsyncMock(return_value=payload)
    )

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert raised.value.failure_code == "bad_response"


def test_fixture_page_parses_into_normalizable_rows(monkeypatch):
    monkeypatch.setattr(
        dart_capital,
        "request_list_page",
        AsyncMock(return_value=_fixture_payload()),
    )

    rows = asyncio.run(dart_helpers.fetch_dart_filings_for_date(DAY))

    assert len(rows) == 3
    assert all(isinstance(row["rcept_dt"], str) for row in rows)
    event, _ = normalize_dart_disclosure_row(rows[0])
    assert event["event_date"] == date(2026, 11, 27)
    assert event["symbol"] == "123456"


# -- closed failure codes -------------------------------------------------------


def _envelope(status: str, **extra) -> bytes:
    return json.dumps({"status": status, "message": "x", **extra}).encode()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("010", "http_4xx"),  # unregistered key
        ("011", "http_4xx"),  # unusable key
        ("012", "http_4xx"),  # IP not allowed
        ("901", "http_4xx"),  # account expired
        ("021", "http_4xx"),  # request argument limit
        ("100", "http_4xx"),  # field value rejected
        ("020", "quota"),  # request limit (retried, then refused)
        ("800", "http_5xx"),  # system maintenance
        (
            "900",
            "bad_response",
        ),  # undefined error: not transient, not classified further
        ("999", "bad_response"),  # a status this client does not know
    ],
)
async def test_list_status_maps_to_a_closed_failure_code(monkeypatch, status, expected):
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "synthetic-key")
    monkeypatch.setattr(dart_capital, "_get_bytes", lambda *_: _envelope(status))
    monkeypatch.setattr(dart_capital.asyncio, "sleep", AsyncMock())

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert raised.value.failure_code == expected


@pytest.mark.asyncio
async def test_no_data_status_is_an_empty_day_not_a_failure(monkeypatch):
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "synthetic-key")
    monkeypatch.setattr(dart_capital, "_get_bytes", lambda *_: _envelope("013"))

    assert await dart_helpers.fetch_dart_filings_for_date(DAY) == []


@pytest.mark.asyncio
async def test_malformed_json_is_a_bad_response(monkeypatch):
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "synthetic-key")
    monkeypatch.setattr(dart_capital, "_get_bytes", lambda *_: b"<html>error</html>")

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert raised.value.failure_code == "bad_response"


@pytest.mark.asyncio
async def test_missing_key_fails_before_any_request(monkeypatch):
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "")
    connection = MagicMock()
    monkeypatch.setattr(dart_capital, "HTTPSConnection", connection)

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert raised.value.failure_code == "http_4xx"
    connection.assert_not_called()


def _connection(*, status=None, error=None):
    class FakeResponse:
        def __init__(self) -> None:
            self.status = status

        def read(self, _size):
            return _envelope("000", list=[], total_page=1)

    class FakeConnection:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def request(self, method, path):
            if error is not None:
                raise error

        def getresponse(self):
            return FakeResponse()

        def close(self) -> None:
            return None

    return FakeConnection


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(404, "http_4xx"), (429, "http_4xx"), (503, "http_5xx")],
)
async def test_http_status_maps_to_4xx_or_5xx(monkeypatch, status, expected):
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "synthetic-key")
    monkeypatch.setattr(dart_capital, "HTTPSConnection", _connection(status=status))

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert raised.value.failure_code == expected


@pytest.mark.asyncio
async def test_timeout_and_transport_are_distinct_closed_codes(monkeypatch):
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", "synthetic-key")

    monkeypatch.setattr(
        dart_capital, "HTTPSConnection", _connection(error=TimeoutError("slow"))
    )
    with pytest.raises(DartDetailError) as timed_out:
        await dart_helpers.fetch_dart_filings_for_date(DAY)
    assert timed_out.value.failure_code == "timeout"

    monkeypatch.setattr(
        dart_capital, "HTTPSConnection", _connection(error=ConnectionResetError())
    )
    with pytest.raises(DartDetailError) as reset:
        await dart_helpers.fetch_dart_filings_for_date(DAY)
    assert reset.value.failure_code == "transport"


@pytest.mark.asyncio
async def test_transport_failure_message_never_carries_the_key(monkeypatch):
    sentinel = "SYNTHETIC_DART_KEY_SENTINEL"
    monkeypatch.setattr(dart_capital.settings, "opendart_api_key", sentinel)
    monkeypatch.setattr(
        dart_capital,
        "HTTPSConnection",
        _connection(error=ConnectionResetError(f"crtfc_key={sentinel}")),
    )

    with pytest.raises(DartDetailError) as raised:
        await dart_helpers.fetch_dart_filings_for_date(DAY)

    assert sentinel not in str(raised.value)
    assert raised.value.__cause__ is None


# -- ingestion records the closed code -------------------------------------------


class _RecordingRepo:
    instances: list[_RecordingRepo] = []

    def __init__(self, _db) -> None:
        self.partitions: dict[date, SimpleNamespace] = {}
        _RecordingRepo.instances.append(self)

    async def get_or_create_partition(self, *, partition_date, **_):
        return self.partitions.setdefault(
            partition_date, SimpleNamespace(last_error=None, status="pending")
        )

    async def mark_partition_running(self, part):
        part.status = "running"

    async def mark_partition_failed(self, part, *, error=None, failure_code=None):
        part.status = "failed"
        part.last_error = failure_code if failure_code is not None else error


@pytest.mark.asyncio
async def test_collection_failure_is_recorded_as_its_closed_code(monkeypatch):
    from app.services.market_events import ingestion

    _RecordingRepo.instances = []
    monkeypatch.setattr(ingestion, "MarketEventsRepository", _RecordingRepo)
    fetch = AsyncMock(
        side_effect=DartDetailError("OpenDART HTTP 503", failure_code="http_5xx")
    )
    db = MagicMock()
    db.rollback = AsyncMock()

    result = await ingestion.ingest_kr_disclosures_for_date(
        db, DAY, fetch_rows=fetch, today=date(2026, 11, 28)
    )

    assert result.status == "failed"
    assert result.error == "http_5xx"
    assert _RecordingRepo.instances[-1].partitions[DAY].last_error == "http_5xx"


# -- normalization: publication precision, amendment family, identity -----------


def _row(**overrides) -> dict:
    row = {
        "corp_code": "00000001",
        "corp_name": "Synthetic Listed Co",
        "stock_code": "123456",
        "corp_cls": "Y",
        "report_nm": "사업보고서 (2025.12)",
        "rcept_no": "20261127000010",
        "flr_nm": "Synthetic Listed Co",
        "rcept_dt": "20261127",
        "rm": "",
    }
    row.update(overrides)
    return row


def test_official_receipt_date_is_date_precision_with_no_time():
    event, _ = normalize_dart_disclosure_row(_row())

    assert event["published_precision"] == "date"
    assert event["published_date"] == date(2026, 11, 27)
    assert event["published_at"] is None
    assert event["published_offset"] is None
    assert event["event_date"] == date(2026, 11, 27)


def test_a_row_that_carries_a_time_keeps_it_as_a_kst_minute_instant():
    event, _ = normalize_dart_disclosure_row(_row(rcept_dt="2026-11-27 16:30"))

    assert event["published_precision"] == "minute"
    assert event["published_date"] is None
    assert event["published_offset"] == "+09:00"
    # The instant is KST-aware; the repository converts to UTC on write.
    assert event["published_at"] == datetime(2026, 11, 27, 16, 30, tzinfo=KST)


def test_a_row_with_seconds_is_second_precision():
    event, _ = normalize_dart_disclosure_row(_row(rcept_dt="2026-11-27T16:30:05"))

    assert event["published_precision"] == "second"
    assert event["published_at"] == datetime(2026, 11, 27, 16, 30, 5, tzinfo=KST)


def test_correction_tags_share_the_family_key_with_the_original():
    original = normalize_dart_disclosure_row(_row())[0]
    correction = normalize_dart_disclosure_row(
        _row(report_nm="[기재정정]사업보고서 (2025.12)", rcept_no="20261127000011")
    )[0]
    second = normalize_dart_disclosure_row(
        _row(report_nm="[첨부정정] 사업보고서 (2025.12)", rcept_no="20261127000012")
    )[0]

    assert original["group_key"] == correction["group_key"] == second["group_key"]
    assert original["group_key"] == "dart:00000001:사업보고서(2025.12)"


def test_different_report_or_issuer_is_a_different_family():
    base = normalize_dart_disclosure_row(_row())[0]["group_key"]
    other_year = normalize_dart_disclosure_row(_row(report_nm="사업보고서 (2026.12)"))[
        0
    ]
    other_issuer = normalize_dart_disclosure_row(_row(corp_code="00000002"))[0]

    assert other_year["group_key"] != base
    assert other_issuer["group_key"] != base


def test_a_reclassified_report_keeps_its_receipt_identity():
    before = normalize_dart_disclosure_row(_row(report_nm="감사인지정"))[0]
    after = normalize_dart_disclosure_row(_row(report_nm="분기보고서 (2026.03)"))[0]

    assert before["source_event_id"] == after["source_event_id"] == "20261127000010"
    assert before["category"] == "disclosure"
    assert after["category"] == "earnings"


def test_official_stock_code_resolves_the_symbol_without_a_registry():
    event, _ = normalize_dart_disclosure_row(_row(), corp_registry=None)

    assert event["symbol"] == "123456"
    assert "symbol_resolution_reason" not in event["raw_payload_json"]


def test_unlisted_issuer_without_a_registry_is_unresolved_not_corp_code():
    event, _ = normalize_dart_disclosure_row(
        _row(stock_code="", corp_code="00009999", corp_cls="E"),
        corp_registry=None,
    )

    assert event["symbol"] is None
    assert event["raw_payload_json"]["symbol_resolution_reason"] == (
        "registry_unavailable"
    )
