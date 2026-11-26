from __future__ import annotations

import asyncio
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.services.market_events import naver_disclosures as naver
from scripts import collect_naver_disclosures, diagnose_kr_sessions

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
        return rows if _page == 1 else []

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
    }
    first = await naver.collect_symbol_disclosures(**args)
    first_received_at = saved["r1"]["first_received_at"]
    args["newest_stored_at"] = datetime(2026, 11, 13, 6, 36, 59, tzinfo=UTC)
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
    nfkc_disclosure = naver.parse_disclosure(
        "005930",
        {
            "disclosureId": "n_nfkc",
            "title": "ABC",
            "datetime": "2026-11-13T09:00:00",
        },
    )
    assert (
        naver.match_dart_receipt(
            nfkc_disclosure,
            [SimpleNamespace(**{**dart.__dict__, "title": "ＡＢＣ"})],
        )
        == "rcept-1"
    )
    assert (
        naver.match_dart_receipt(
            naver.parse_disclosure(
                "005930",
                {
                    "disclosureId": "n2",
                    "title": "ABC",
                    "datetime": "2026-11-13T09:00:00",
                },
            ),
            [SimpleNamespace(**{**dart.__dict__, "title": "abc"})],
        )
        is None
    )
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
    )
    assert page_calls == [1, 2]
    assert result == {"pages": 2, "observed": 2, "inserted": 2}


@pytest.mark.asyncio
async def test_pagination_has_a_hard_five_page_cap() -> None:
    calls: list[int] = []
    row = {"disclosureId": "n1", "title": "공시", "datetime": "2026-11-14T10:00:00"}

    async def fetch(_symbol: str, page: int) -> list[dict[str, object]]:
        calls.append(page)
        return [row]

    async def unused_insert(_event: dict[str, object]) -> bool:
        pytest.fail("dry-run attempted a write")

    result = await naver.collect_symbol_disclosures(
        symbol="005930",
        fetch_page=fetch,
        insert_event=unused_insert,
        newest_stored_at=None,
        received_at=lambda: datetime(2026, 11, 15, tzinfo=UTC),
        commit=False,
        max_pages=99,
    )
    assert calls == [1, 2, 3, 4, 5]
    assert result == {"pages": 5, "observed": 5, "inserted": 0}


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
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "naver_sessions"
        / "2026-11-26.json"
    )
    fixture = json.loads(fixture_path.read_text())
    response = naver.parse_sessions(fixture)
    assert response.server_time is None
    assert response.trade_dates == {
        ("krx", "KOSPI"): date(2026, 11, 26),
        ("krx", "KOSDAQ"): date(2026, 11, 26),
        ("nxt", "KOSPI"): date(2026, 11, 26),
        ("nxt", "KOSDAQ"): date(2026, 11, 26),
    }
    assert len(response.windows) == 10
    assert all(window.trade_date == date(2026, 11, 26) for window in response.windows)
    assert all(window.label for window in response.windows)
    assert all(
        window.open_at.astimezone(naver._KST).date() == window.trade_date
        for window in response.windows
    )

    def fake_segment(_market: str, venue: str, at: datetime) -> str:
        local = at.astimezone(naver._KST)
        if venue == "KRX" and local.hour == 9:
            return "KRX_REGULAR"
        return naver.UNKNOWN

    monkeypatch.setattr(naver, "_session_segment", fake_segment, raising=False)
    verdicts = naver.compare_session_windows(date(2026, 11, 26), response)
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
async def test_sessions_diagnostic_preserves_provider_date_and_counts_one_sided_as_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture_path = (
        Path(__file__).resolve().parents[2]
        / "fixtures"
        / "naver_sessions"
        / "2026-11-26.json"
    )
    snapshot = naver.parse_sessions(json.loads(fixture_path.read_text()))

    class Client:
        async def sessions(self) -> naver.SessionResponse:
            return snapshot

    monkeypatch.setattr(naver, "_session_segment", lambda *_args: naver.UNKNOWN)
    report, code = await diagnose_kr_sessions.run(date(2026, 11, 26), client=Client())
    assert code == 1
    assert any(row["verdict"] == "naver_only" for row in report["comparisons"])
    assert report["server_time"] is None
    assert all(
        "tradeBaseAt=2026-11-26;" in row["naver"]
        for row in report["comparisons"]
        if row["verdict"] == "naver_only"
    )

    report, code = await diagnose_kr_sessions.run(date(2026, 11, 22), client=Client())
    assert code == 0
    assert {row["verdict"] for row in report["comparisons"]} == {"unavailable"}


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


@pytest.mark.asyncio
async def test_collector_reports_one_symbol_failure_and_continues() -> None:
    class Result:
        def all(self) -> list[Any]:
            return []

        def scalars(self) -> Result:
            return self

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, _statement: object) -> Result:
            return Result()

        async def rollback(self) -> None:
            return None

    class Client:
        async def disclosures(self, symbol: str, *, page: int) -> list[dict[str, Any]]:
            assert page == 1
            if symbol == "005930":
                raise naver.NaverDisclosureHttpError("fixture failure")
            return []

    report = await collect_naver_disclosures.run(
        collect_naver_disclosures._parse_args(
            ["--symbols", "005930", "000660", "--dry-run"]
        ),
        client=Client(),  # type: ignore[arg-type]
        session_factory=Session,
    )
    assert report["errors"] == [
        {"symbol": "005930", "error_class": "NaverDisclosureHttpError"}
    ]
    assert report["results"] == [
        {"symbol": "000660", "pages": 1, "observed": 0, "inserted": 0}
    ]


@pytest.mark.asyncio
async def test_collector_dart_match_survives_rollback_from_another_symbol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.models.market_events import MarketEvent
    from app.services.market_events import repository as repository_module

    class Result:
        def __init__(
            self, rows: list[Any] | None = None, scalars: list[Any] | None = None
        ):
            self._rows = rows or []
            self._scalars = scalars or []

        def all(self) -> list[Any]:
            return self._rows

        def scalars(self) -> Result:
            return Result(rows=self._scalars)

        def scalar_one_or_none(self) -> int:
            return 1

    class ExpiringDartRow:
        def __init__(self, session: Any) -> None:
            self._session = session

        def __getattr__(self, name: str) -> Any:
            if self._session.rollback_count:
                raise RuntimeError("expired ORM attribute accessed after rollback")
            return {
                "source": "dart",
                "symbol": "000660",
                "event_date": date(2026, 11, 26),
                "title": "실적 공시",
                "source_event_id": "dart-rcept-2",
            }[name]

    class Session:
        def __init__(self) -> None:
            self.rollback_count = 0
            self.commit_count = 0
            self.events: list[dict[str, Any]] = []

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: Any) -> Result:
            columns = statement.column_descriptions
            if len(columns) == 2:
                return Result(rows=[])
            if len(columns) == 1 and columns[0]["entity"] is MarketEvent:
                return Result(scalars=[ExpiringDartRow(self)])
            if len(columns) == 5:
                return Result(
                    rows=[
                        (
                            "dart",
                            "000660",
                            date(2026, 11, 26),
                            "실적 공시",
                            "dart-rcept-2",
                        )
                    ]
                )
            raise AssertionError(f"unexpected query shape: {columns!r}")

        async def rollback(self) -> None:
            self.rollback_count += 1

        async def commit(self) -> None:
            self.commit_count += 1

    session = Session()

    class Repo:
        def __init__(self, db: Session) -> None:
            self.db = db

        async def insert_provider_event(self, event: dict[str, Any]) -> bool:
            self.db.events.append(event)
            return True

    monkeypatch.setattr(repository_module, "MarketEventsRepository", Repo)

    class Client:
        async def disclosures(self, symbol: str, *, page: int) -> list[dict[str, Any]]:
            if symbol == "005930":
                raise naver.NaverDisclosureHttpError("fixture failure")
            if page == 1:
                return [
                    {
                        "disclosureId": "n2",
                        "title": "실적 공시",
                        "datetime": "2026-11-26T09:00:00",
                    }
                ]
            return []

    report = await collect_naver_disclosures.run(
        collect_naver_disclosures._parse_args(
            ["--symbols", "005930", "000660", "--commit"]
        ),
        client=Client(),  # type: ignore[arg-type]
        session_factory=lambda: session,
    )
    assert report["errors"] == [
        {"symbol": "005930", "error_class": "NaverDisclosureHttpError"}
    ]
    assert report["results"] == [
        {"symbol": "000660", "pages": 2, "observed": 1, "inserted": 1}
    ]
    assert session.rollback_count == 1
    assert session.commit_count == 1
    assert session.events[0]["raw_payload_json"]["dart_rcept_no"] == "dart-rcept-2"


@pytest.mark.asyncio
async def test_collector_returns_target_errors_for_empty_target_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.brokers.kis import client as kis_client_module
    from app.services.research_candles import session_minutes_collector

    class Result:
        def all(self) -> list[Any]:
            return []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    async def failed_targets(**_kwargs: Any) -> Any:
        return SimpleNamespace(
            symbols=[],
            errors=[{"origin": "fixed_symbols", "error_class": "ValueError"}],
        )

    monkeypatch.setattr(session_minutes_collector, "build_targets", failed_targets)
    monkeypatch.setattr(kis_client_module, "KISClient", lambda: object())
    report = await collect_naver_disclosures.run(
        collect_naver_disclosures._parse_args([]),
        client=SimpleNamespace(aclose=lambda: asyncio.sleep(0)),
        session_factory=Session,
    )
    assert report["symbols"] == []
    assert report["errors"] == [
        {"origin": "fixed_symbols", "error_class": "ValueError"}
    ]


@pytest.mark.asyncio
async def test_client_serializes_requests_at_two_per_second() -> None:
    now = [0.0]
    request_times: list[float] = []

    async def sleep(seconds: float) -> None:
        now[0] += seconds

    def handle(_request: httpx.Request) -> httpx.Response:
        request_times.append(now[0])
        return httpx.Response(200, json=[])

    raw = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    client = naver.NaverPublicClient(
        client=raw,
        monotonic=lambda: now[0],
        sleep=sleep,
    )
    await asyncio.gather(
        client.disclosures("005930", page=1),
        client.disclosures("000660", page=1),
    )
    assert len(request_times) == 2
    assert request_times[1] - request_times[0] >= 0.5
    await raw.aclose()


@pytest.mark.asyncio
async def test_client_rejects_redirect_without_following_it() -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://m.stock.naver.com/elsewhere"}
        )

    raw = httpx.AsyncClient(
        transport=httpx.MockTransport(handle), follow_redirects=True
    )
    client = naver.NaverPublicClient(client=raw)
    with pytest.raises(naver.NaverDisclosureHttpError, match="redirects"):
        await client.disclosures("005930", page=1)
    await raw.aclose()


@pytest.mark.asyncio
async def test_sessions_diagnostic_exit_codes(monkeypatch: pytest.MonkeyPatch) -> None:
    class Client:
        async def sessions(self) -> naver.SessionResponse:
            return naver.SessionResponse(
                datetime(2026, 11, 13, 6, tzinfo=UTC),
                {("krx", "KOSPI"): date(2026, 11, 13)},
                [
                    naver.SessionWindow(
                        "krx",
                        "KOSPI",
                        "정규장",
                        datetime(2026, 11, 13, 0, tzinfo=UTC),
                        datetime(2026, 11, 13, 6, 31, tzinfo=UTC),
                        date(2026, 11, 13),
                    )
                ],
            )

    monkeypatch.setattr(
        naver,
        "_session_segment",
        lambda _market, _venue, at: (
            "KRX_REGULAR" if at.astimezone(naver._KST).hour == 9 else naver.UNKNOWN
        ),
    )
    report, code = await diagnose_kr_sessions.run(date(2026, 11, 13), client=Client())
    assert code == 1
    assert any(row["verdict"] == "mismatch" for row in report["comparisons"])

    class Unavailable:
        async def sessions(self) -> naver.SessionResponse:
            raise naver.NaverDisclosureHttpError("fixture unavailable")

    report, code = await diagnose_kr_sessions.run(
        date(2026, 11, 13), client=Unavailable()
    )
    assert code == 0
    assert {row["verdict"] for row in report["comparisons"]} == {"unavailable"}
