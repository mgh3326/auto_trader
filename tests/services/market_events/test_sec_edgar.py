"""SEC EDGAR collector: gate, client contract, normalization, idempotency."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.services.market_events.sec_edgar import (
    ALLOWED_HOSTS,
    CATEGORY,
    IR_UNSUPPORTED_REASON,
    MARKET,
    REQUEST_INTERVAL_SECONDS,
    SEC_FORMS,
    SEC_SOURCE,
    SecEdgarClient,
    SecEdgarError,
    SecEdgarUserAgentMissing,
    _filing_rows,
    _window_complete,
    collect_ir_requests,
    collect_sec_filings,
    index_company_tickers,
    normalize_sec_filing,
)

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "sec_edgar"
RECEIVED = datetime(2026, 11, 27, 12, 0, tzinfo=UTC)
RUN_DATE = date(2026, 11, 27)


def fixture(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


TICKERS = fixture("company_tickers.json")
NVDA_SUBMISSIONS = fixture("submissions_nvda.json")
EMPTY_SUBMISSIONS = fixture("submissions_empty.json")


@pytest.fixture(autouse=True)
def _user_agent(monkeypatch):
    monkeypatch.setattr(settings, "sec_edgar_user_agent", "Test Agent test@example.com")


class FakeSession:
    def __init__(self):
        self.commits = 0
        self.rollbacks = 0

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class FakePartition:
    def __init__(self, **kwargs):
        self.status = "pending"
        self.event_count = 0
        self.last_error = None
        self.retry_count = 0
        self.started_at = None
        self.finished_at = None
        self.source_request_hash = None
        for key, value in kwargs.items():
            setattr(self, key, value)


class FakeRepository:
    def __init__(self):
        self.db = FakeSession()
        self.events = {}
        self.revisions = {}
        self.sources = {}
        self.partitions = {}
        self.running_marks = 0

    async def get_or_create_partition(
        self, *, source, category, market, partition_date
    ):
        key = (source, category, market, partition_date)
        if key not in self.partitions:
            self.partitions[key] = FakePartition(
                source=source,
                category=category,
                market=market,
                partition_date=partition_date,
            )
        return self.partitions[key]

    async def mark_partition_running(self, partition):
        self.running_marks += 1
        partition.status = "running"
        partition.started_at = datetime.now(UTC)
        partition.last_error = None

    async def mark_partition_pending(self, partition):
        partition.status = "pending"
        partition.event_count = 0
        partition.last_error = None

    async def mark_partition_succeeded(self, partition, *, event_count):
        partition.status = "succeeded"
        partition.event_count = event_count
        partition.finished_at = datetime.now(UTC)
        partition.last_error = None

    async def mark_partition_failed(self, partition, *, error=None, failure_code=None):
        assert (error is None) != (failure_code is None)
        partition.status = "failed"
        partition.last_error = failure_code or error
        partition.finished_at = datetime.now(UTC)
        partition.retry_count = (partition.retry_count or 0) + 1

    async def latest_revision(self, *, source, category, market, source_event_id):
        rows = self.revisions.get((source, source_event_id), [])
        return rows[-1] if rows else None

    async def append_revision(self, **record):
        key = (record["source"], record["source_event_id"])
        rows = self.revisions.setdefault(key, [])
        rows.append(SimpleNamespace(**record))
        return True

    async def lock_revision_identity(self, **_identity):
        return None

    async def upsert_event_with_values(self, event, values, **_kwargs):
        key = (event["source"], event["source_event_id"])
        prior = self.events.get(key)
        if prior:
            event = {**event, "first_received_at": prior["first_received_at"]}
        stored = dict(event)
        stored["id"] = len(self.events) + 1 if key not in self.events else prior["id"]
        self.events[key] = stored
        return SimpleNamespace(id=stored["id"])

    async def get_provider_event(self, *, source, category, market, source_event_id):
        stored = self.events.get((source, source_event_id))
        return SimpleNamespace(**stored) if stored is not None else None

    async def append_source(self, source_data):
        key = (source_data["source"], source_data["source_event_id"])
        if key in self.sources:
            return False
        self.sources[key] = dict(source_data)
        return True


class FakeEdgarClient:
    """Serves fixture payloads; records calls; can inject per-CIK failures."""

    def __init__(self, *, submissions=None, tickers=TICKERS, failures=None):
        self._submissions = dict(submissions or {})
        self._tickers = tickers
        self._failures = dict(failures or {})
        self.calls = []

    async def fetch_company_tickers(self):
        self.calls.append(("tickers", None))
        return self._tickers

    async def fetch_submissions(self, cik):
        self.calls.append(("submissions", cik))
        if cik in self._failures:
            raise self._failures[cik]
        return self._submissions.get(cik, EMPTY_SUBMISSIONS)


def _nvda_client(**kwargs):
    return FakeEdgarClient(submissions={1045810: NVDA_SUBMISSIONS}, **kwargs)


# -- User-Agent gate ---------------------------------------------------------


def test_empty_user_agent_refused_at_client_construction():
    with pytest.raises(SecEdgarUserAgentMissing):
        SecEdgarClient(user_agent="")
    with pytest.raises(SecEdgarUserAgentMissing):
        SecEdgarClient(user_agent="   ")


@pytest.mark.asyncio
async def test_collect_refuses_empty_user_agent_before_repo(monkeypatch):
    monkeypatch.setattr(settings, "sec_edgar_user_agent", "")
    repo = FakeRepository()
    client = FakeEdgarClient()
    with pytest.raises(SecEdgarUserAgentMissing):
        await collect_sec_filings(
            None, symbols=["NVDA"], repo=repo, client=client, commit=True
        )
    assert client.calls == []
    assert repo.partitions == {}


# -- HTTP client contract ----------------------------------------------------


class _FakeResponse:
    def __init__(self, status=200, body=None, content_type="application/json"):
        self.status_code = status
        self.headers = {"content-type": content_type}
        self.content = (
            body if isinstance(body, bytes) else json.dumps(body or {}).encode()
        )


class _FakeAsyncClient:
    """Async-context-manager httpx.AsyncClient stand-in."""

    handler = None
    constructed_kwargs = None

    def __init__(self, **kwargs):
        type(self).constructed_kwargs = kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, **kwargs):
        type(self).requests.append({"url": url, **kwargs})
        return type(self).handler(url)

    requests = []


def _factory(handler):
    _FakeAsyncClient.handler = staticmethod(handler)
    _FakeAsyncClient.requests = []

    def factory(**kwargs):
        return _FakeAsyncClient(**kwargs)

    return factory


@pytest.mark.asyncio
async def test_client_sends_user_agent_and_never_follows_redirects():
    client = SecEdgarClient(
        user_agent="Declared Agent",
        client_factory=_factory(lambda url: _FakeResponse(body={"ok": True})),
    )
    await client.fetch_company_tickers()
    request = _FakeAsyncClient.requests[0]
    assert request["headers"]["User-Agent"] == "Declared Agent"
    assert _FakeAsyncClient.constructed_kwargs["follow_redirects"] is False


@pytest.mark.asyncio
async def test_client_rejects_non_allowlisted_or_non_https_url():
    client = SecEdgarClient(
        user_agent="Declared Agent",
        client_factory=_factory(lambda url: _FakeResponse(body={})),
    )
    for bad in ("http://data.sec.gov/x", "https://evil.example.com/x"):
        with pytest.raises(SecEdgarError):
            await client._get_json(bad)
    assert ALLOWED_HOSTS == frozenset({"www.sec.gov", "data.sec.gov"})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (404, "http_4xx"),
        (403, "http_4xx"),
        (500, "http_5xx"),
        (503, "http_5xx"),
        (429, "quota"),
        (302, "bad_response"),
    ],
)
async def test_http_status_maps_to_closed_failure_code(status, expected):
    client = SecEdgarClient(
        user_agent="Declared Agent",
        client_factory=_factory(lambda url: _FakeResponse(status=status, body={})),
    )
    with pytest.raises(SecEdgarError) as excinfo:
        await client.fetch_company_tickers()
    assert excinfo.value.failure_code == expected


@pytest.mark.asyncio
async def test_timeout_and_transport_map_to_closed_codes():
    def raising(url):
        raise httpx.TimeoutException("slow")

    client = SecEdgarClient(
        user_agent="Declared Agent", client_factory=_factory(raising)
    )
    with pytest.raises(SecEdgarError) as excinfo:
        await client.fetch_company_tickers()
    assert excinfo.value.failure_code == "timeout"

    def conn_error(url):
        raise httpx.ConnectError("refused")

    client = SecEdgarClient(
        user_agent="Declared Agent", client_factory=_factory(conn_error)
    )
    with pytest.raises(SecEdgarError) as excinfo:
        await client.fetch_company_tickers()
    assert excinfo.value.failure_code == "transport"


@pytest.mark.asyncio
async def test_bad_json_and_non_json_content_type_are_closed_failures():
    client = SecEdgarClient(
        user_agent="Declared Agent",
        client_factory=_factory(lambda url: _FakeResponse(body=b"not-json{{{")),
    )
    with pytest.raises(SecEdgarError) as excinfo:
        await client.fetch_company_tickers()
    assert excinfo.value.failure_code == "unparseable"

    client = SecEdgarClient(
        user_agent="Declared Agent",
        client_factory=_factory(
            lambda url: _FakeResponse(body=b"<html></html>", content_type="text/html")
        ),
    )
    with pytest.raises(SecEdgarError) as excinfo:
        await client.fetch_company_tickers()
    assert excinfo.value.failure_code == "bad_response"


@pytest.mark.asyncio
async def test_requests_are_paced_at_five_per_second_or_slower():
    now = [1000.0]
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)
        now[0] += seconds

    client = SecEdgarClient(
        user_agent="Declared Agent",
        client_factory=_factory(lambda url: _FakeResponse(body={"ok": True})),
        sleep=fake_sleep,
        monotonic=lambda: now[0],
        min_interval=REQUEST_INTERVAL_SECONDS,
    )
    await client.fetch_company_tickers()
    await client.fetch_company_tickers()
    assert slept and all(0 < s <= REQUEST_INTERVAL_SECONDS for s in slept)
    assert REQUEST_INTERVAL_SECONDS >= 0.2  # <= 5 req/s


# -- company_tickers indexing -------------------------------------------------


def test_index_company_tickers_normalizes_and_validates():
    index = index_company_tickers(TICKERS)
    assert index["NVDA"] == (1045810, "NVIDIA CORP")
    assert index["AAPL"] == (320193, "Apple Inc.")
    with pytest.raises(SecEdgarError) as excinfo:
        index_company_tickers(["not", "a", "dict"])
    assert excinfo.value.failure_code == "unparseable"


# -- submissions parsing -------------------------------------------------------


def test_filing_rows_split_parallel_arrays_and_report_older_history():
    rows, meta = _filing_rows(NVDA_SUBMISSIONS)
    assert len(rows) == 5
    assert rows[0]["accessionNumber"] == "0001045810-25-000301"
    assert rows[0]["form"] == "8-K"
    assert rows[0]["primaryDocument"] == "nvda-20251119.htm"
    # Real submissions JSON shape: ISO-8601 UTC, not the SGML 14-digit form.
    assert rows[0]["acceptanceDateTime"] == "2025-11-19T21:31:12.000Z"
    assert rows[4]["acceptanceDateTime"] == ""
    assert meta["older_history_uncollected"] is True
    assert meta["recent_earliest"] == date(2025, 2, 26)


def test_filing_rows_requires_core_columns():
    for field in ("form", "filingDate", "acceptanceDateTime"):
        broken = copy.deepcopy(NVDA_SUBMISSIONS)
        del broken["filings"]["recent"][field]
        with pytest.raises(SecEdgarError) as excinfo:
            _filing_rows(broken)
        assert excinfo.value.failure_code == "unparseable"


def test_filing_rows_empty_recent_is_zero_filings_not_error():
    rows, meta = _filing_rows(EMPTY_SUBMISSIONS)
    assert rows == []
    assert meta["older_history_uncollected"] is False


def test_window_complete_rules():
    # No explicit window → recent scope is complete by definition.
    assert _window_complete(
        older_history_uncollected=True,
        recent_earliest=date(2025, 2, 26),
        from_date=None,
        to_date=None,
    )
    # No older chunks → any window is covered.
    assert _window_complete(
        older_history_uncollected=False,
        recent_earliest=None,
        from_date=date(1990, 1, 1),
        to_date=None,
    )
    # Window inside recent coverage → complete.
    assert _window_complete(
        older_history_uncollected=True,
        recent_earliest=date(2025, 2, 26),
        from_date=date(2025, 6, 1),
        to_date=None,
    )
    # Window reaching before coverage → unproven.
    assert not _window_complete(
        older_history_uncollected=True,
        recent_earliest=date(2025, 2, 26),
        from_date=date(2020, 1, 1),
        to_date=None,
    )
    assert not _window_complete(
        older_history_uncollected=True,
        recent_earliest=None,
        from_date=date(2020, 1, 1),
        to_date=date(2021, 1, 1),
    )


# -- normalization -------------------------------------------------------------


def _row(index: int) -> dict:
    rows, _meta = _filing_rows(NVDA_SUBMISSIONS)
    return rows[index]


def test_normalize_8k_second_precision_iso_utc_to_et():
    event = normalize_sec_filing(
        _row(0),
        cik=1045810,
        symbol="NVDA",
        symbols=["NVDA"],
        company_name="NVIDIA CORP",
        received_at=RECEIVED,
    )
    assert event["source"] == "sec"
    assert event["source_event_id"] == "0001045810-25-000301"
    assert event["category"] == "disclosure"
    assert event["market"] == "us"
    assert event["symbol"] == "NVDA"
    assert event["symbols"] == ["NVDA"]
    assert event["company_name"] == "NVIDIA CORP"
    assert event["title"] == "8-K (items 2.02,9.01)"
    assert event["event_date"] == date(2025, 11, 19)
    # acceptanceDateTime "2025-11-19T21:31:12.000Z" is UTC; the filing index
    # page shows the same instant as 16:31:12 US Eastern (EST, UTC-5).
    assert event["published_at"] == datetime(2025, 11, 19, 21, 31, 12, tzinfo=UTC)
    assert event["published_offset"] == "-05:00"
    assert event["published_precision"] == "second"
    assert event["published_date"] is None
    assert event["release_time_local"] == datetime(2025, 11, 19, 16, 31, 12)
    assert event["source_url"] == (
        "https://www.sec.gov/Archives/edgar/data/1045810/"
        "000104581025000301/nvda-20251119.htm"
    )
    assert event["status"] == "released"
    assert event["first_received_at"] == RECEIVED
    assert event["backfilled"] is True
    assert event["instrument_type"] == "equity_us"
    assert event["raw_payload_json"]["accessionNumber"] == "0001045810-25-000301"


def test_parse_acceptance_iso_utc_and_sgml_fallback():
    from app.services.market_events.sec_edgar import _parse_acceptance

    # ISO-8601 UTC (the real submissions JSON shape) → UTC instant + ET offset.
    utc, offset, local = _parse_acceptance("2026-11-27T18:06:41.000Z")
    assert utc == datetime(2026, 11, 27, 18, 6, 41, tzinfo=UTC)
    assert offset == "-05:00"  # EST at that instant
    assert local == datetime(2026, 11, 27, 13, 6, 41)

    # Explicit offset form parses the same way.
    utc2, _, _ = _parse_acceptance("2026-11-27T13:06:41-05:00")
    assert utc2 == utc

    # SGML-header 14-digit form is US Eastern wall time.
    utc3, offset3, local3 = _parse_acceptance("20251119163112")
    assert utc3 == datetime(2025, 11, 19, 21, 31, 12, tzinfo=UTC)
    assert offset3 == "-05:00"
    assert local3 == datetime(2025, 11, 19, 16, 31, 12)

    for bad in ("", "not-a-date", "2025-13-40", "2025-11-19", None, 123):
        assert _parse_acceptance(bad) is None


def test_backfilled_is_derived_from_filing_day_not_flag():
    rows, _meta = _filing_rows(NVDA_SUBMISSIONS)
    # Filing day 2025-11-19 collected on 2026-11-27 → backfilled.
    old = normalize_sec_filing(
        rows[0],
        cik=1045810,
        symbol="NVDA",
        symbols=["NVDA"],
        company_name="NVIDIA CORP",
        received_at=RECEIVED,
    )
    assert old["backfilled"] is True
    # Same-day collection is not a backfill.
    same_day = normalize_sec_filing(
        rows[0],
        cik=1045810,
        symbol="NVDA",
        symbols=["NVDA"],
        company_name="NVIDIA CORP",
        received_at=datetime(2025, 11, 19, 22, 0, tzinfo=UTC),
    )
    assert same_day["backfilled"] is False


def test_normalize_10q_uses_edt_offset():
    event = normalize_sec_filing(
        _row(1),
        cik=1045810,
        symbol="NVDA",
        symbols=["NVDA"],
        company_name="NVIDIA CORP",
        received_at=RECEIVED,
    )
    # 2025-08-27 16:32:01 EDT (UTC-4) → 20:32:01 UTC
    assert event["published_at"] == datetime(2025, 8, 27, 20, 32, 1, tzinfo=UTC)
    assert event["published_offset"] == "-04:00"
    assert event["title"] == "10-Q"


def test_normalize_missing_acceptance_falls_back_to_date_precision():
    row = {**_row(0), "acceptanceDateTime": ""}
    event = normalize_sec_filing(
        row,
        cik=1045810,
        symbol="NVDA",
        symbols=["NVDA"],
        company_name="NVIDIA CORP",
        received_at=RECEIVED,
    )
    assert event["published_precision"] == "date"
    assert event["published_date"] == date(2025, 11, 19)
    assert event["published_at"] is None
    assert event["published_offset"] is None
    assert event["release_time_utc"] is None


def test_normalize_rejects_missing_accession_and_unknown_form():
    with pytest.raises(SecEdgarError) as excinfo:
        normalize_sec_filing(
            {**_row(0), "accessionNumber": ""},
            cik=1045810,
            symbol="NVDA",
            symbols=None,
            company_name=None,
            received_at=RECEIVED,
        )
    assert excinfo.value.failure_code == "unparseable"
    with pytest.raises(SecEdgarError):
        normalize_sec_filing(
            {**_row(0), "form": "13F-HR"},
            cik=1045810,
            symbol="NVDA",
            symbols=None,
            company_name=None,
            received_at=RECEIVED,
        )


def test_normalize_cik_only_issuer_has_no_symbol_relevance():
    event = normalize_sec_filing(
        _row(2),
        cik=1045810,
        symbol=None,
        symbols=None,
        company_name="NVIDIA CORP",
        received_at=RECEIVED,
    )
    assert event["symbol"] is None
    assert event["symbols"] is None
    assert event["relevance_state"] == "unknown"
    assert event["instrument_type"] is None


# -- collection: dry-run, commit, idempotency ---------------------------------


@pytest.mark.asyncio
async def test_dry_run_collects_and_reports_without_writes():
    repo = FakeRepository()
    client = _nvda_client()
    report = await collect_sec_filings(
        None,
        symbols=["NVDA", "FAKE"],
        repo=repo,
        client=client,
        commit=False,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["dry_run"] is True
    assert report["partition"] is None
    nvda = report["issuers"]["NVDA"]
    assert nvda["status"] == "collected"
    assert nvda["filings"] == 4  # 8-K, 10-Q, 10-K, 8-K/A — form 4 excluded
    assert nvda["symbol"] == "NVDA"
    assert nvda["older_history_uncollected"] is True
    assert nvda["window_complete"] is True
    assert report["issuers"]["FAKE"] == {
        "status": "unresolved",
        "reason": "ticker_not_listed",
    }
    assert report["totals"]["unresolved"] == 1
    assert report["totals"]["filings"] == 4
    assert repo.events == {}
    # ticker map fetched once, submissions once per resolved symbol
    assert client.calls == [("tickers", None), ("submissions", 1045810)]


@pytest.mark.asyncio
async def test_commit_writes_rows_and_recollection_adds_no_duplicates():
    repo = FakeRepository()
    client = _nvda_client()
    first = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    # A symbol-scoped run can never prove the source-wide day complete, so a
    # clean run leaves the partition pending — never succeeded, never a
    # false "no filings" zero (hk 6407 review; Benzinga --symbols precedent).
    assert first["partition"]["status"] == "pending"
    assert first["partition"]["event_count"] == 4
    assert first["partition"]["persisted"] is True
    assert first["totals"]["inserted"] == 4
    assert len(repo.events) == 4
    assert len(repo.sources) == 4
    accession = "0001045810-25-000301"
    event = repo.events[("sec", accession)]
    assert event["published_at"] == datetime(2025, 11, 19, 21, 31, 12, tzinfo=UTC)
    assert event["published_precision"] == "second"
    assert event["published_offset"] == "-05:00"
    assert event["first_received_at"] == RECEIVED
    assert repo.sources[("sec", accession)]["raw_hash"]
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "pending"
    assert partition.event_count == 0

    # Re-collecting the same scope on the same run_date: no duplicate rows,
    # no duplicate provenance, and the partition stays pending — a second
    # scoped read must not flip it to succeeded+0 "no filings".
    second = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=_nvda_client(),
        commit=True,
        clock=lambda: RECEIVED + timedelta(hours=1),
        run_date=RUN_DATE,
    )
    assert second["totals"]["unchanged"] == 4
    assert second["totals"]["inserted"] == 0
    assert second["partition"]["status"] == "pending"
    assert len(repo.events) == 4  # no duplicate rows
    assert len(repo.sources) == 4  # provenance is first-observation only
    assert len(repo.revisions[("sec", accession)]) == 1
    # first_received_at is insert-only — never moved by re-collection
    assert repo.events[("sec", accession)]["first_received_at"] == RECEIVED
    assert repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)].status == (
        "pending"
    )


@pytest.mark.asyncio
async def test_fetch_failure_marks_partition_failed_distinct_from_no_filings():
    repo = FakeRepository()
    tickers = {
        **TICKERS,
        "3": {"cik_str": 320193, "ticker": "BADCO", "title": "Bad Co"},
    }
    client = FakeEdgarClient(
        submissions={1045810: NVDA_SUBMISSIONS, 320193: EMPTY_SUBMISSIONS},
        tickers=tickers,
        failures={320193: SecEdgarError("boom", failure_code="http_5xx")},
    )
    report = await collect_sec_filings(
        None,
        symbols=["NVDA", "BADCO"],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["issuers"]["NVDA"]["status"] == "collected"
    assert report["issuers"]["BADCO"]["status"] == "failed"
    assert report["issuers"]["BADCO"]["failure_code"] == "http_5xx"
    assert report["partition"]["status"] == "failed"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "failed"
    assert partition.last_error == "http_5xx"
    # NVDA rows were still stored — failure is not "no filings"
    assert len(repo.events) == 4


@pytest.mark.asyncio
async def test_scoped_success_never_completes_shared_partition():
    """A different symbol's success must not clear another scope's failure,
    and only the failing scope's own clean re-run clears it."""
    repo = FakeRepository()
    tickers = {
        **TICKERS,
        "3": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."},
    }
    failing = FakeEdgarClient(
        submissions={1045810: NVDA_SUBMISSIONS, 320193: EMPTY_SUBMISSIONS},
        tickers=tickers,
        failures={1045810: SecEdgarError("boom", failure_code="http_5xx")},
    )
    first = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=failing,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert first["partition"]["status"] == "failed"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "failed"
    assert partition.last_error == "http_5xx"
    nvda_failure_map = json.loads(partition.source_request_hash)
    assert len(nvda_failure_map) == 1

    # A same-day AAPL run succeeds — but it is a different scope, so the
    # shared day partition must stay failed: the NVDA gap is still real.
    ok = FakeEdgarClient(submissions={320193: EMPTY_SUBMISSIONS}, tickers=tickers)
    second = await collect_sec_filings(
        None,
        symbols=["AAPL"],
        repo=repo,
        client=ok,
        commit=True,
        clock=lambda: RECEIVED + timedelta(hours=2),
        run_date=RUN_DATE,
    )
    assert second["partition"]["status"] == "failed"
    assert partition.status == "failed"
    assert partition.last_error == "http_5xx"
    assert json.loads(partition.source_request_hash) == nvda_failure_map

    # Only the failing scope's own clean re-run clears the failure.
    recovered = FakeEdgarClient(
        submissions={1045810: NVDA_SUBMISSIONS}, tickers=tickers
    )
    third = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=recovered,
        commit=True,
        clock=lambda: RECEIVED + timedelta(hours=4),
        run_date=RUN_DATE,
    )
    assert third["partition"]["status"] == "pending"
    assert partition.status == "pending"
    assert partition.source_request_hash is None


@pytest.mark.asyncio
async def test_empty_recent_is_pending_zero_not_failure():
    repo = FakeRepository()
    tickers = {
        **TICKERS,
        "3": {"cik_str": 1, "ticker": "ESPY", "title": "EMPTY HOLDINGS CORP"},
    }
    client = FakeEdgarClient(
        submissions={1: EMPTY_SUBMISSIONS},
        tickers=tickers,
    )
    report = await collect_sec_filings(
        None,
        symbols=["ESPY"],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["issuers"]["ESPY"]["status"] == "collected"
    assert report["issuers"]["ESPY"]["filings"] == 0
    # Scoped "zero filings" is pending, not failed and not a claimed
    # source-wide "no filings" (succeeded+0 is unreachable here).
    assert report["partition"]["status"] == "pending"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "pending"


@pytest.mark.asyncio
async def test_window_reaching_before_recent_coverage_stays_pending():
    repo = FakeRepository()
    report = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=_nvda_client(),
        commit=True,
        from_date=date(2020, 1, 1),
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["partition"]["status"] == "pending"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "pending"


@pytest.mark.asyncio
async def test_cik_mode_resolves_symbol_from_submissions():
    client = _nvda_client()
    report = await collect_sec_filings(
        None,
        ciks=[1045810],
        repo=FakeRepository(),
        client=client,
        commit=False,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    issuer = report["issuers"]["CIK0001045810"]
    assert issuer["status"] == "collected"
    assert issuer["symbol"] == "NVDA"
    assert issuer["filings"] == 4
    # no ticker-map fetch needed for pure CIK runs
    assert client.calls == [("submissions", 1045810)]


@pytest.mark.asyncio
async def test_ticker_map_failure_fails_partition_with_closed_code():
    repo = FakeRepository()

    class BrokenTickers(FakeEdgarClient):
        async def fetch_company_tickers(self):
            raise SecEdgarError("down", failure_code="http_5xx")

    report = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=BrokenTickers(),
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["error"]["failure_code"] == "http_5xx"
    assert report["partition"]["status"] == "failed"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.last_error == "http_5xx"


@pytest.mark.asyncio
async def test_all_unresolved_symbols_is_pending_not_no_filings():
    repo = FakeRepository()
    report = await collect_sec_filings(
        None,
        symbols=["FAKE"],
        repo=repo,
        client=_nvda_client(),
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["issuers"]["FAKE"]["status"] == "unresolved"
    # A typo'd scope proves nothing about the day — pending, not failed and
    # never a claimed "no filings".
    assert report["partition"]["status"] == "pending"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "pending"
    assert partition.last_error is None


@pytest.mark.asyncio
async def test_unparseable_rows_fail_partition_but_keep_parsed_rows():
    repo = FakeRepository()
    broken = copy.deepcopy(NVDA_SUBMISSIONS)
    recent = broken["filings"]["recent"]
    # First row is an in-window 8-K; strip both date fields so normalize
    # cannot place it — a parse failure, not an out-of-window filing.
    recent["filingDate"][0] = ""
    recent["acceptanceDateTime"][0] = ""
    client = FakeEdgarClient(submissions={1045810: broken})
    report = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    issuer = report["issuers"]["NVDA"]
    assert issuer["status"] == "failed"
    assert issuer["failure_code"] == "unparseable"
    assert issuer["skipped_unparseable"] == 1
    assert issuer["filings"] == 3  # the rows that did parse stay stored
    assert report["partition"]["status"] == "failed"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "failed"
    assert partition.last_error == "unparseable"
    assert len(repo.events) == 3


@pytest.mark.asyncio
async def test_missing_form_column_fails_instead_of_silent_zero():
    repo = FakeRepository()
    broken = copy.deepcopy(NVDA_SUBMISSIONS)
    del broken["filings"]["recent"]["form"]
    client = FakeEdgarClient(submissions={1045810: broken})
    report = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert report["issuers"]["NVDA"]["status"] == "failed"
    assert report["issuers"]["NVDA"]["failure_code"] == "unparseable"
    assert report["partition"]["status"] == "failed"
    assert repo.events == {}


@pytest.mark.asyncio
async def test_db_failure_rolls_back_and_records_closed_last_error():
    """A write error must not poison the shared transaction or leak text."""

    class ExplodingRepository(FakeRepository):
        async def upsert_event_with_values(self, event, values, **kwargs):
            raise RuntimeError("pg_terminate: SELECT secret_column FROM t")

    repo = ExplodingRepository()
    report = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=_nvda_client(),
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    issuer = report["issuers"]["NVDA"]
    assert issuer["status"] == "failed"
    assert issuer["failure_code"] == "bad_response"
    assert report["partition"]["status"] == "failed"
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "failed"
    # Closed code only — raw exception text never reaches last_error (§2.4).
    assert partition.last_error == "bad_response"
    assert "secret_column" not in (partition.last_error or "")
    assert repo.db.rollbacks >= 1


@pytest.mark.asyncio
async def test_joint_filing_merges_symbols_without_flipflop():
    """One accession under two CIKs unions symbols; content stays put."""
    repo = FakeRepository()
    client = FakeEdgarClient(
        submissions={
            111: _joint_payload(111, "AAA", "Alpha Corp", "001-1", "111"),
            222: _joint_payload(222, "BBB", "Beta Corp", "001-2", "222"),
        }
    )
    for cik in (111, 222, 111, 222):
        await collect_sec_filings(
            None,
            ciks=[cik],
            repo=repo,
            client=client,
            commit=True,
            clock=lambda: RECEIVED,
            run_date=RUN_DATE,
        )
    assert len(repo.events) == 1
    event = repo.events[("sec", "0000000001-25-000001")]
    # First writer keeps the primary fields; symbols is the union.
    assert event["symbol"] == "AAA"
    assert event["company_name"] == "Alpha Corp"
    assert event["symbols"] == ["AAA", "BBB"]
    assert event["source_url"].startswith(
        "https://www.sec.gov/Archives/edgar/data/111/"
    )
    # Content-identical re-reads append no revisions — no flip-flop.
    assert len(repo.revisions[("sec", "0000000001-25-000001")]) == 1


@pytest.mark.asyncio
async def test_db_session_must_match_repository_session():
    repo = FakeRepository()
    with pytest.raises(ValueError, match="db must match the repository session"):
        await collect_sec_filings(
            FakeSession(),
            symbols=["NVDA"],
            repo=repo,
            client=_nvda_client(),
            commit=True,
            clock=lambda: RECEIVED,
            run_date=RUN_DATE,
        )


@pytest.mark.asyncio
async def test_stale_observation_still_merges_symbols():
    """A co-registrant read that observed earlier must not lose its ticker."""
    repo = FakeRepository()
    client = FakeEdgarClient(
        submissions={
            111: _joint_payload(111, "AAA", "Alpha Corp", "001-1", "111"),
            222: _joint_payload(222, "BBB", "Beta Corp", "001-2", "222"),
        }
    )
    later = datetime(2026, 11, 27, 18, 0, tzinfo=UTC)
    earlier = later - timedelta(hours=2)
    # CIK 111's observation is the newer one and owns the content.
    await collect_sec_filings(
        None,
        ciks=[111],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: later,
        run_date=RUN_DATE,
    )
    # CIK 222 observed the same accession earlier — stale content, but its
    # symbol must still union into the stored row.
    report = await collect_sec_filings(
        None,
        ciks=[222],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: earlier,
        run_date=RUN_DATE,
    )
    assert report["totals"]["stale"] == 1
    event = repo.events[("sec", "0000000001-25-000001")]
    assert event["symbols"] == ["AAA", "BBB"]
    assert event["symbol"] == "AAA"
    assert event["company_name"] == "Alpha Corp"
    # No revision for the stale observation; content untouched.
    assert len(repo.revisions[("sec", "0000000001-25-000001")]) == 1


@pytest.mark.asyncio
async def test_window_applies_to_normalized_event_date():
    """A blank filingDate falls back to the acceptance date — the window
    bound must be checked on the stored event_date, not just the raw row."""
    repo = FakeRepository()
    broken = copy.deepcopy(NVDA_SUBMISSIONS)
    # Row 1 (10-Q) has acceptance 2025-08-27; blank its filingDate so
    # _in_window keeps it and normalize dates it from acceptance.
    broken["filings"]["recent"]["filingDate"][1] = ""
    client = FakeEdgarClient(submissions={1045810: broken})
    report = await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=client,
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
        from_date=date(2025, 11, 19),
        to_date=date(2025, 11, 19),
    )
    # Only the 8-K filed 2025-11-19 survives; the acceptance-dated 10-Q
    # (2025-08-27) is outside the window despite the blank filingDate.
    assert report["issuers"]["NVDA"]["filings"] == 1
    assert len(repo.events) == 1
    assert repo.events[("sec", "0001045810-25-000301")]["event_date"] == date(
        2025, 11, 19
    )


@pytest.mark.asyncio
async def test_partition_running_claim_is_never_persisted():
    """Per-issuer commits must not carry a 'running' partition claim — a
    kill mid-run would leave the day stuck 'running' forever."""
    repo = FakeRepository()
    await collect_sec_filings(
        None,
        symbols=["NVDA"],
        repo=repo,
        client=_nvda_client(),
        commit=True,
        clock=lambda: RECEIVED,
        run_date=RUN_DATE,
    )
    assert repo.running_marks == 0
    partition = repo.partitions[(SEC_SOURCE, CATEGORY, MARKET, RUN_DATE)]
    assert partition.status == "pending"


def _joint_payload(cik: int, ticker: str, name: str, file_no: str, film: str):
    return {
        "cik": f"{cik:010d}",
        "name": name,
        "tickers": [ticker],
        "filings": {
            "recent": {
                "accessionNumber": ["0000000001-25-000001"],
                "filingDate": ["2025-11-19"],
                "reportDate": ["2025-09-30"],
                "acceptanceDateTime": ["2025-11-19T21:31:12.000Z"],
                "act": ["34-13376"],
                "form": ["10-Q"],
                "fileNumber": [file_no],
                "filmNumber": [film],
                "items": [""],
                "core_type": [""],
                "size": [1234],
                "isXBRL": [1],
                "isInlineXBRL": [1],
                "isForeignPrivateIssuer": [0],
                "primaryDocument": [f"{ticker.lower()}-10q.htm"],
                "primaryDocDescription": ["10-Q"],
            },
            "files": [],
        },
    }


# -- IR ------------------------------------------------------------------------


def test_ir_requests_are_unsupported_without_any_network():
    result = collect_ir_requests(["AAPL", " MSFT ", ""])
    assert result == {
        "AAPL": {"status": "unsupported", "reason": IR_UNSUPPORTED_REASON},
        "MSFT": {"status": "unsupported", "reason": IR_UNSUPPORTED_REASON},
    }
    assert "scraping" in IR_UNSUPPORTED_REASON


def test_sec_source_is_in_taxonomy():
    from app.services.market_events.taxonomy import SOURCES

    assert "sec" in SOURCES
    assert SEC_SOURCE == "sec"
    assert CATEGORY == "disclosure"
    assert MARKET == "us"
    assert SEC_FORMS == frozenset({"8-K", "10-Q", "10-K", "8-K/A", "10-Q/A", "10-K/A"})
