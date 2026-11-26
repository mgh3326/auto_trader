"""Recorded Upbit/Bithumb notices exercise normalization and idempotency."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.market_events.crypto_notices import (
    BITHUMB_SOURCE,
    UPBIT_SOURCE,
    CryptoNoticeClient,
    CryptoNoticeContractError,
    CryptoNoticeHTTPError,
    _write_notice,
    _request_json,
    classify_notice_kind,
    collect_crypto_notices,
    extract_ticker,
    normalize_bithumb_notice,
    normalize_upbit_notice,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "crypto_notices"
RECEIVED = datetime(2026, 11, 26, 1, 0, tzinfo=UTC)


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


UPBIT_NOTICE = fixture("upbit_page.json")["data"]["notices"][0]
BITHUMB_NOTICE = fixture("bithumb_notices.json")[0]


class FakeRepository:
    def __init__(self):
        self.events = {}
        self.revisions = {}

    async def list_source_event_ids(self, *, source, category, market):
        return {
            event_id
            for (event_source, event_id) in self.events
            if event_source == source
        }

    async def latest_revision(self, *, source, category, market, source_event_id):
        rows = self.revisions.get((source, source_event_id), [])
        return rows[-1] if rows else None

    async def append_revision(self, **record):
        key = (record["source"], record["source_event_id"])
        rows = self.revisions.setdefault(key, [])
        rows.append(SimpleNamespace(**record))
        return True

    async def upsert_event_with_values(
        self, event, values, *, clear_notice_symbol=False
    ):
        key = (event["source"], event["source_event_id"])
        prior = self.events.get(key)
        if prior:
            event = {**event, "first_received_at": prior["first_received_at"]}
            if clear_notice_symbol:
                event = {**event, "symbol": None}
            elif event.get("symbol") is None:
                event = {**event, "symbol": prior.get("symbol")}
        self.events[key] = dict(event)

    async def lock_revision_identity(self, **_identity):
        return None


class FakeClient:
    upbit_tickers = {"STABLE"}

    def __init__(
        self,
        upbit_rows=None,
        bithumb_rows=None,
        markets=None,
        *,
        upbit_pages=None,
        upbit_total_pages=2,
    ):
        self.upbit_rows = list(upbit_rows if upbit_rows is not None else [UPBIT_NOTICE])
        self.upbit_page_rows = upbit_pages or {}
        self.upbit_total_pages = upbit_total_pages
        self.bithumb_rows = list(
            bithumb_rows if bithumb_rows is not None else [BITHUMB_NOTICE]
        )
        self.markets = set(markets if markets is not None else {"STABLE"})
        self.pages = []
        self.bithumb_calls = 0

    async def fetch_upbit_page(self, page):
        self.pages.append(page)
        return (
            self.upbit_page_rows.get(page, self.upbit_rows if page == 1 else []),
            self.upbit_total_pages,
            [],
        )

    async def fetch_bithumb(self):
        self.bithumb_calls += 1
        return self.bithumb_rows, self.markets


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def execute(self, _statement):
        return SimpleNamespace(scalars=lambda: iter(()))

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


def test_recorded_shapes_keep_provider_timestamps_categories_and_exact_tickers():
    upbit = normalize_upbit_notice(
        UPBIT_NOTICE, known_tickers={"STABLE"}, received_at=RECEIVED
    )
    assert upbit["source"] == UPBIT_SOURCE
    assert upbit["source_event_id"] == "88001"
    assert upbit["symbol"] == "STABLE"
    assert upbit["provider_created_at"].isoformat() == "2026-11-26T01:00:00+00:00"
    assert upbit["provider_updated_at"].isoformat() == "2026-11-26T01:05:00+00:00"
    assert upbit["raw_payload_json"]["categories"] == ["거래"]
    assert upbit["raw_payload_json"]["notice_kind"] == "listing"

    bithumb = normalize_bithumb_notice(
        BITHUMB_NOTICE, known_tickers={"STABLE"}, received_at=RECEIVED
    )
    assert bithumb["source"] == BITHUMB_SOURCE
    assert bithumb["source_event_id"] == "23001"
    assert bithumb["symbol"] == "STABLE"
    assert bithumb["provider_created_at"].isoformat() == "2026-11-26T01:00:00+00:00"
    assert bithumb["provider_updated_at"].isoformat() == "2026-11-26T01:05:00+00:00"
    assert bithumb["raw_payload_json"]["categories"] == ["입출금"]
    assert bithumb["raw_payload_json"]["notice_kind"] == "deposit_withdrawal_suspend"


def test_ticker_requires_one_explicit_parenthesized_market_ticker():
    assert extract_ticker("신규 거래지원(STABLE)", {"STABLE"}) == "STABLE"
    assert extract_ticker("스테이블 신규 거래지원", {"STABLE"}) is None
    assert extract_ticker("신규 거래지원(OTHER)", {"STABLE"}) is None
    assert extract_ticker("(STABLE) (OTHER)", {"STABLE", "OTHER"}) is None
    assert extract_ticker("(stable)", {"STABLE"}) is None


@pytest.mark.parametrize(
    ("title", "categories", "expected"),
    [
        ("신규 거래지원 안내", ["거래"], "listing"),
        ("거래지원 종료 안내", ["거래"], "delisting"),
        ("유의종목 지정 안내", ["거래"], "warning_designation"),
        ("유의종목 해제 안내", ["거래"], "warning_release"),
        ("유의종목 지정 해제 안내", ["거래"], "warning_release"),
        ("입출금 일시 중단 안내", ["입출금"], "deposit_withdrawal_suspend"),
        ("입출금 재개 안내", ["입출금"], "deposit_withdrawal_resume"),
        ("점검 안내", ["안내"], "other"),
    ],
)
def test_notice_kind_uses_closed_phrase_rules(title, categories, expected):
    assert classify_notice_kind(title, categories) == expected


@pytest.mark.parametrize(
    ("title", "categories", "expected"),
    [
        ("코인 입출금 일시 중지 안내", ["입출금"], "deposit_withdrawal_suspend"),
        ("스테이블 원화 마켓 추가", ["마켓추가"], "listing"),
    ],
)
def test_notice_kind_recognizes_bithumb_phrases(title, categories, expected):
    assert classify_notice_kind(title, categories) == expected


@pytest.mark.asyncio
async def test_collection_stops_at_stored_id_and_preserves_first_received_on_rerun():
    repo = FakeRepository()
    client = FakeClient()
    first = await collect_crypto_notices(
        None, pages=4, repo=repo, client=client, commit=True, received_at=RECEIVED
    )
    assert first[UPBIT_SOURCE]["inserted"] == 1
    assert first[BITHUMB_SOURCE]["inserted"] == 1
    assert client.pages == [1, 2]

    later = datetime(2026, 11, 26, 2, 0, tzinfo=UTC)
    rerun = await collect_crypto_notices(
        None, pages=4, repo=repo, client=client, commit=True, received_at=later
    )
    assert rerun[UPBIT_SOURCE]["stopped_at_known_id"] == 1
    assert rerun[BITHUMB_SOURCE]["stopped_at_known_id"] == 1
    assert len(repo.revisions[(UPBIT_SOURCE, "88001")]) == 1
    assert repo.events[(UPBIT_SOURCE, "88001")]["first_received_at"] == RECEIVED
    assert client.pages == [1, 2, 1]


@pytest.mark.asyncio
async def test_modified_provider_record_appends_revision_and_updates_current_row():
    repo = FakeRepository()
    await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(),
        commit=True,
        received_at=RECEIVED,
    )
    revised = {**BITHUMB_NOTICE, "modified_at": "2026-11-26 10:10:00"}
    report = await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(upbit_rows=[UPBIT_NOTICE], bithumb_rows=[revised]),
        commit=True,
        received_at=datetime(2026, 11, 26, 2, 0, tzinfo=UTC),
    )
    assert report[BITHUMB_SOURCE]["revised"] == 1
    assert len(repo.revisions[(BITHUMB_SOURCE, "23001")]) == 2
    current = repo.events[(BITHUMB_SOURCE, "23001")]
    assert current["provider_updated_at"].isoformat() == "2026-11-26T01:10:00+00:00"
    assert current["first_received_at"] == RECEIVED


@pytest.mark.asyncio
async def test_bithumb_checks_revisions_after_the_known_id_boundary():
    repo = FakeRepository()
    await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(bithumb_rows=[BITHUMB_NOTICE]),
        commit=True,
        received_at=RECEIVED,
    )
    later_notice = {**BITHUMB_NOTICE, "pc_url": "https://feed.bithumb.com/notice/23002"}
    revised_older = {
        **BITHUMB_NOTICE,
        "modified_at": "2026-11-26 10:10:00",
    }
    result = await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(bithumb_rows=[later_notice, revised_older]),
        commit=True,
        received_at=datetime(2026, 11, 26, 2, 0, tzinfo=UTC),
    )
    assert result[BITHUMB_SOURCE]["stopped_at_known_id"] == 1
    assert result[BITHUMB_SOURCE]["revised"] == 1
    assert len(repo.revisions[(BITHUMB_SOURCE, "23001")]) == 2


@pytest.mark.asyncio
async def test_older_observation_cannot_replace_a_newer_revision():
    repo = FakeRepository()
    newer_payload = {**BITHUMB_NOTICE, "modified_at": "2026-11-26 10:10:00"}
    newer = normalize_bithumb_notice(
        newer_payload, known_tickers={"STABLE"}, received_at=RECEIVED
    )
    await _write_notice(repo, newer, newer_payload, observed_at=RECEIVED)

    older_payload = {**BITHUMB_NOTICE, "modified_at": "2026-11-26 10:02:00"}
    older = normalize_bithumb_notice(
        older_payload, known_tickers={"STABLE"}, received_at=RECEIVED
    )
    status = await _write_notice(
        repo,
        older,
        older_payload,
        observed_at=datetime(2026, 11, 26, 0, 59, tzinfo=UTC),
    )

    assert status == "stale"
    assert len(repo.revisions[(BITHUMB_SOURCE, "23001")]) == 1
    assert repo.events[(BITHUMB_SOURCE, "23001")]["provider_updated_raw"] == (
        "2026-11-26 10:10:00"
    )


@pytest.mark.asyncio
async def test_revision_clears_symbol_only_when_title_no_longer_has_one_ticker():
    repo = FakeRepository()
    await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(),
        commit=True,
        received_at=RECEIVED,
    )
    corrected = {**BITHUMB_NOTICE, "title": "거래소 안내문 수정"}
    await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(bithumb_rows=[corrected]),
        commit=True,
        received_at=datetime(2026, 11, 26, 2, 0, tzinfo=UTC),
    )
    assert repo.events[(BITHUMB_SOURCE, "23001")]["symbol"] is None


@pytest.mark.asyncio
async def test_upbit_fixed_notices_are_processed_without_ending_normal_pagination():
    repo = FakeRepository()
    client = FakeClient(
        upbit_pages={1: [UPBIT_NOTICE]}, upbit_total_pages=1
    )
    original = client.fetch_upbit_page

    async def with_fixed(page):
        rows, total_pages, _fixed = await original(page)
        fixed = {**UPBIT_NOTICE, "id": 88000}
        return rows, total_pages, [fixed]

    client.fetch_upbit_page = with_fixed
    result = await collect_crypto_notices(
        None, repo=repo, client=client, commit=True, received_at=RECEIVED
    )
    assert result[UPBIT_SOURCE]["inserted"] == 2
    assert set(repo.events) >= {
        (UPBIT_SOURCE, "88000"),
        (UPBIT_SOURCE, "88001"),
    }


@pytest.mark.asyncio
async def test_empty_store_bootstraps_through_page_cap_and_bithumb_independently():
    repo = FakeRepository()
    session = FakeSession()
    page_one = {**UPBIT_NOTICE, "id": 88001}
    page_two = {**UPBIT_NOTICE, "id": 88002}
    client = FakeClient(
        upbit_pages={1: [page_one], 2: [page_two]}, upbit_total_pages=3
    )

    first = await collect_crypto_notices(
        session,
        pages=2,
        repo=repo,
        client=client,
        commit=True,
        received_at=RECEIVED,
    )

    assert first[UPBIT_SOURCE]["inserted"] == 2
    assert first[UPBIT_SOURCE]["truncated"] is True
    assert first[UPBIT_SOURCE]["page_cap"] == 2
    assert first[BITHUMB_SOURCE]["inserted"] == 1
    assert client.bithumb_calls == 1
    assert session.commits == 2
    assert session.rollbacks == 0

    rerun_client = FakeClient(
        upbit_pages={1: [page_one], 2: [page_two]}, upbit_total_pages=3
    )
    second = await collect_crypto_notices(
        session,
        pages=2,
        repo=repo,
        client=rerun_client,
        commit=True,
        received_at=RECEIVED,
    )
    assert second[UPBIT_SOURCE]["inserted"] == 0
    assert second[BITHUMB_SOURCE]["inserted"] == 0
    assert len(repo.events) == 3
    assert len(repo.revisions[(UPBIT_SOURCE, "88001")]) == 1
    assert rerun_client.pages == [1]


@pytest.mark.asyncio
async def test_upbit_fetch_failure_does_not_prevent_bithumb_collection():
    repo = FakeRepository()
    client = FakeClient()

    async def fail_upbit(_page):
        raise CryptoNoticeHTTPError("provider returned HTTP 503")

    client.fetch_upbit_page = fail_upbit
    report = await collect_crypto_notices(
        None, repo=repo, client=client, commit=True, received_at=RECEIVED
    )

    assert report[UPBIT_SOURCE]["error"] == "CryptoNoticeHTTPError"
    assert report[BITHUMB_SOURCE]["inserted"] == 1
    assert client.bithumb_calls == 1


@pytest.mark.asyncio
async def test_full_bithumb_window_without_known_id_reports_possible_gap():
    rows = [
        {**BITHUMB_NOTICE, "pc_url": f"https://feed.bithumb.com/notice/{24000 + i}"}
        for i in range(20)
    ]
    result = await collect_crypto_notices(
        None,
        repo=FakeRepository(),
        client=FakeClient(bithumb_rows=rows),
        received_at=RECEIVED,
    )
    assert result[BITHUMB_SOURCE]["truncated"] is True
    assert result[BITHUMB_SOURCE]["page_cap"] == 20
    assert result[BITHUMB_SOURCE]["older_history_uncollected"] is True


@pytest.mark.asyncio
async def test_upbit_client_sends_required_category_referer_and_refuses_redirects():
    calls = []

    class Response:
        status_code = 302
        headers = {"content-type": "application/json"}

    class Client:
        def __init__(self, **kwargs):
            calls.append(("client", kwargs))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            calls.append((url, params, headers))
            return Response()

    client = CryptoNoticeClient(client_factory=Client)
    with pytest.raises(CryptoNoticeHTTPError, match="redirect refused"):
        await client.fetch_upbit_page(1)
    assert calls[0][1]["follow_redirects"] is False
    assert calls[1][1]["category"] == "notice"
    assert calls[1][2]["Referer"] == "https://upbit.com/"


@pytest.mark.asyncio
async def test_bithumb_client_requests_the_largest_supported_notice_window():
    calls = []

    class Response:
        status_code = 200
        headers = {"content-type": "application/json"}

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            calls.append((url, params))
            if url.endswith("/market/all"):
                return Response([{"market": "KRW-STABLE"}])
            return Response([])

    await CryptoNoticeClient(client_factory=Client).fetch_bithumb()
    assert calls[1][1] == {"count": 20}


@pytest.mark.asyncio
async def test_fetch_requires_allowlisted_host_and_json_content_type():
    class Response:
        status_code = 200
        headers = {"content-type": "text/html"}

        def json(self):
            return {}

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, *, params, headers):
            return Response()

    with pytest.raises(CryptoNoticeContractError, match="host allowlist"):
        await _request_json(
            "https://example.com/notices",
            expected_host="api.bithumb.com",
            client_factory=Client,
        )
    with pytest.raises(CryptoNoticeContractError, match="not JSON"):
        await _request_json(
            "https://api.bithumb.com/v1/notices",
            expected_host="api.bithumb.com",
            client_factory=Client,
        )


def test_cli_returns_failure_code_for_collection_error(monkeypatch, caplog):
    import scripts.collect_crypto_notices as cli

    async def fail(args):
        raise CryptoNoticeHTTPError("provider returned HTTP 503")

    monkeypatch.setattr(cli, "_run", fail)
    assert cli.main(["--pages", "1", "--dry-run"]) == 1
    assert "CryptoNoticeHTTPError" in caplog.text


def test_dagu_copy_is_manual_only_and_runs_every_five_minutes():
    path = Path("deploy/dagu/at-crypto-notices/at-crypto-notices.yaml")
    dag = path.read_text(encoding="utf-8")
    assert '"*/5 * * * *"' in dag
    assert "scripts.collect_crypto_notices --commit" in dag
    assert "schedule:" in dag  # file is a copy; registration is intentionally absent.
    assert not Path("deploy/dagu/dags/at-crypto-notices.yaml").exists()
