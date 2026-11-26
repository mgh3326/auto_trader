from __future__ import annotations

from datetime import UTC, date, datetime
from types import SimpleNamespace

import httpx
import pytest

from app.services.market_events import naver_disclosures as naver

pytestmark = pytest.mark.unit


def test_zone_less_provider_datetime_is_kst_and_raw_is_kept() -> None:
    disclosure = naver.parse_disclosure(
        "005930",
        {
            "itemCode": "005930",
            "disclosureId": "20261113001234",
            "title": "삼성전자 분기보고서",
            "datetime": "2026-11-13T15:36:59",
            "author": "삼성전자",
        },
    )
    assert disclosure.provider_created_at == datetime(
        2026, 11, 13, 6, 36, 59, tzinfo=UTC
    )
    assert disclosure.event_date == date(2026, 11, 13)
    event = naver.disclosure_event(
        disclosure,
        first_received_at=datetime(2026, 11, 13, 7, tzinfo=UTC),
    )
    assert event["provider_created_raw"] == "2026-11-13T15:36:59"
    assert event["raw_payload_json"]["datetime"] == "2026-11-13T15:36:59"


@pytest.mark.asyncio
async def test_rerun_is_insert_only_and_preserves_first_received_at() -> None:
    saved: dict[str, dict[str, object]] = {}
    rows = [
        {
            "disclosureId": "r1",
            "title": "  삼성전자   분기보고서 ",
            "datetime": "2026-11-13T15:36:59",
        }
    ]

    async def fetch(_symbol: str, _page: int) -> list[dict[str, object]]:
        return rows

    async def insert(event: dict[str, object]) -> bool:
        key = str(event["source_event_id"])
        if key in saved:
            return False
        saved[key] = event
        return True

    dart = SimpleNamespace(
        source="dart",
        symbol="005930",
        event_date=date(2026, 11, 13),
        title="삼성전자 분기보고서",
        source_event_id="dart-rcept-1",
    )
    times = iter(
        [datetime(2026, 11, 13, 7, tzinfo=UTC), datetime(2026, 11, 14, 7, tzinfo=UTC)]
    )
    args = {
        "symbol": "005930",
        "fetch_page": fetch,
        "insert_event": insert,
        "newest_stored_at": None,
        "received_at": lambda: next(times),
        "dart_rows": [dart],
        "commit": True,
        "page_size": 20,
    }
    first = await naver.collect_symbol_disclosures(**args)
    first_received_at = saved["r1"]["first_received_at"]
    second = await naver.collect_symbol_disclosures(**args)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert saved["r1"]["first_received_at"] == first_received_at
    assert saved["r1"]["raw_payload_json"]["dart_rcept_no"] == "dart-rcept-1"


def test_dart_receipt_requires_symbol_date_and_normalized_title_match() -> None:
    disclosure = naver.parse_disclosure(
        "005930",
        {"disclosureId": "n1", "title": "공시 제목", "datetime": "2026-11-13T09:00:00"},
    )
    dart = SimpleNamespace(
        source="dart",
        symbol="005930",
        event_date=date(2026, 11, 13),
        title=" 공시   제목 ",
        source_event_id="rcept-1",
    )
    assert naver.match_dart_receipt(disclosure, [dart]) == "rcept-1"
    assert (
        naver.match_dart_receipt(
            disclosure,
            [dart, SimpleNamespace(**{**dart.__dict__, "source_event_id": "rcept-2"})],
        )
        is None
    )
    assert (
        naver.match_dart_receipt(
            disclosure, [SimpleNamespace(**{**dart.__dict__, "symbol": "000660"})]
        )
        is None
    )


@pytest.mark.asyncio
async def test_pagination_stops_at_first_page_not_newer_than_frontier() -> None:
    page_calls: list[int] = []
    inserted: list[dict[str, object]] = []

    async def fetch(_symbol: str, page: int) -> list[dict[str, object]]:
        page_calls.append(page)
        if page == 1:
            return [
                {
                    "disclosureId": "new",
                    "title": "신규",
                    "datetime": "2026-11-14T10:00:00",
                }
            ]
        return [
            {"disclosureId": "old", "title": "기존", "datetime": "2026-11-13T10:00:00"}
        ]

    async def insert(event: dict[str, object]) -> bool:
        inserted.append(event)
        return True

    result = await naver.collect_symbol_disclosures(
        symbol="005930",
        fetch_page=fetch,
        insert_event=insert,
        newest_stored_at=datetime(2026, 11, 13, 2, tzinfo=UTC),
        received_at=lambda: datetime(2026, 11, 15, tzinfo=UTC),
        commit=True,
        page_size=1,
    )
    assert page_calls == [1, 2]
    assert result == {"pages": 2, "inserted": 2}


@pytest.mark.asyncio
async def test_client_refuses_non_allowlisted_host_before_request() -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: pytest.fail("network called"))
    )
    naver_client = naver.NaverPublicClient(client=client)
    with pytest.raises(naver.NaverDisclosureError, match="not allowed"):
        await naver_client._get_json("https://example.com/data", params={})
    await client.aclose()


def test_sessions_fixture_and_diagnostic_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = {
        "serverTime": "2026-11-13T15:00:00+09:00",
        "exchanges": [
            {
                "exchange": "krx",
                "markets": [
                    {
                        "market": "KOSPI",
                        "sessions": [
                            {
                                "label": "정규장",
                                "openAt": "2026-11-13T09:00:00+09:00",
                                "closeAt": "2026-11-13T15:31:00+09:00",
                            }
                        ],
                    }
                ],
            }
        ],
    }
    server_time, windows = naver.parse_sessions(fixture)
    assert server_time == datetime(2026, 11, 13, 6, tzinfo=UTC)
    assert windows[0].open_at.isoformat() == "2026-11-13T00:00:00+00:00"

    def fake_segment(_market: str, venue: str, at: datetime) -> str:
        local = at.astimezone(naver._KST)
        if venue == "KRX" and local.hour == 9:
            return "KRX_REGULAR"
        return naver.UNKNOWN

    monkeypatch.setattr(naver, "session_segment_of", fake_segment)
    verdicts = naver.compare_session_windows(date(2026, 11, 13), windows)
    assert any(
        row["verdict"] == "mismatch" and row["exchange"] == "krx" for row in verdicts
    )
    assert {row["verdict"] for row in verdicts} <= {
        "match",
        "mismatch",
        "naver_only",
        "ours_only",
        "unavailable",
    }


@pytest.mark.asyncio
async def test_client_requires_json_content_type_and_does_not_follow_redirects() -> (
    None
):
    async def non_json(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="[]", headers={"content-type": "text/plain"})

    transport = httpx.MockTransport(non_json)
    raw = httpx.AsyncClient(transport=transport, follow_redirects=True)
    client = naver.NaverPublicClient(client=raw)
    with pytest.raises(naver.NaverDisclosureResponseError, match="not JSON"):
        await client.disclosures("005930", page=1)
    await raw.aclose()
