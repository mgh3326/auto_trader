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

    async def upsert_event_with_values(self, event, values):
        key = (event["source"], event["source_event_id"])
        prior = self.events.get(key)
        if prior:
            event = {**event, "first_received_at": prior["first_received_at"]}
        self.events[key] = dict(event)


class FakeClient:
    upbit_tickers = {"STABLE"}

    def __init__(self, upbit_rows=None, bithumb_rows=None, markets=None):
        self.upbit_rows = list(upbit_rows if upbit_rows is not None else [UPBIT_NOTICE])
        self.bithumb_rows = list(
            bithumb_rows if bithumb_rows is not None else [BITHUMB_NOTICE]
        )
        self.markets = set(markets if markets is not None else {"STABLE"})
        self.pages = []

    async def fetch_upbit_page(self, page):
        self.pages.append(page)
        return (self.upbit_rows if page == 1 else []), 2

    async def fetch_bithumb(self):
        return self.bithumb_rows, self.markets


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
        client=FakeClient(bithumb_rows=[]),
        commit=True,
        received_at=RECEIVED,
    )
    revised = {**UPBIT_NOTICE, "listed_at": "2026-11-26T10:10:00+09:00"}
    report = await collect_crypto_notices(
        None,
        repo=repo,
        client=FakeClient(upbit_rows=[revised], bithumb_rows=[]),
        commit=True,
        received_at=datetime(2026, 11, 26, 2, 0, tzinfo=UTC),
    )
    assert report[UPBIT_SOURCE]["revised"] == 1
    assert len(repo.revisions[(UPBIT_SOURCE, "88001")]) == 2
    current = repo.events[(UPBIT_SOURCE, "88001")]
    assert current["provider_updated_at"].isoformat() == "2026-11-26T01:10:00+00:00"
    assert current["first_received_at"] == RECEIVED


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
