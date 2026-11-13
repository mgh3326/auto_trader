"""DART disclosure ingestion for a day that is not complete yet (hk 3845).

Pure unit tests: a fake repository stands in for the database, the clock is
injected, and no OpenDART network call is made.
"""

from __future__ import annotations

import json
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pandas as pd
import pytest

from app.services.market_events import dart_helpers, ingestion

TODAY = date(2026, 11, 10)  # Tuesday, confirmed XKRX session
YESTERDAY = date(2026, 11, 9)  # Monday, confirmed XKRX session
SATURDAY = date(2026, 11, 14)


class FakeRepo:
    instances: list[FakeRepo] = []

    def __init__(self, db):
        self.partitions: dict[date, SimpleNamespace] = {}
        self.calls: list[tuple[str, date | None]] = []
        self.events: list[dict] = []
        FakeRepo.instances.append(self)

    async def get_or_create_partition(self, *, partition_date, **_):
        part = self.partitions.setdefault(
            partition_date,
            SimpleNamespace(
                status="pending", event_count=0, last_error=None, day=partition_date
            ),
        )
        return part

    async def mark_partition_running(self, part):
        part.status = "running"
        self.calls.append(("running", part.day))

    async def mark_partition_pending(self, part):
        part.status = "pending"
        part.event_count = 0
        part.last_error = None
        self.calls.append(("pending", part.day))

    async def mark_partition_succeeded(self, part, *, event_count):
        part.status = "succeeded"
        part.event_count = event_count
        self.calls.append(("succeeded", part.day))

    async def mark_partition_failed(self, part, *, error):
        part.status = "failed"
        part.last_error = error
        self.calls.append(("failed", part.day))

    async def upsert_event_with_values(self, event, _values):
        self.events.append(event)


@pytest.fixture
def repo(monkeypatch):
    FakeRepo.instances = []
    monkeypatch.setattr(ingestion, "MarketEventsRepository", FakeRepo)
    return FakeRepo


def _db():
    db = MagicMock()
    db.rollback = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_today_zero_filings_is_pending_not_complete(repo):
    result = await ingestion.ingest_kr_disclosures_for_date(
        _db(),
        TODAY,
        fetch_rows=AsyncMock(return_value=[]),
        today=TODAY,
    )

    assert result.status == "pending"
    assert result.event_count == 0
    assert result.error is None
    fake = repo.instances[0]
    assert fake.partitions[TODAY].status == "pending"
    assert [name for name, _ in fake.calls] == ["running", "pending"]
    assert not any(name in ("succeeded", "failed") for name, _ in fake.calls)


@pytest.mark.asyncio
async def test_past_confirmed_session_zero_filings_still_fails(repo):
    result = await ingestion.ingest_kr_disclosures_for_date(
        _db(),
        YESTERDAY,
        fetch_rows=AsyncMock(return_value=[]),
        today=TODAY,
    )

    assert result.status == "failed"
    assert "DART returned zero filings on confirmed XKRX trading session" in (
        result.error or ""
    )
    assert repo.instances[-1].partitions[YESTERDAY].status == "failed"


def test_guard_raises_runtime_error_for_a_completed_session():
    with pytest.raises(
        RuntimeError,
        match="DART returned zero filings on confirmed XKRX trading session 2026-11-09",
    ):
        ingestion._require_legitimate_dart_zero(YESTERDAY, 0)
    assert ingestion._require_legitimate_dart_zero(TODAY, 0, may_be_pending=True)


@pytest.mark.asyncio
async def test_today_with_unknown_calendar_still_fails(repo, monkeypatch):
    monkeypatch.setattr(ingestion, "trading_session_status", lambda *_: "unknown")

    result = await ingestion.ingest_kr_disclosures_for_date(
        _db(),
        TODAY,
        fetch_rows=AsyncMock(return_value=[]),
        today=TODAY,
    )

    assert result.status == "failed"
    assert "calendar classification is unknown" in (result.error or "")


@pytest.mark.asyncio
async def test_today_on_confirmed_non_session_is_complete(repo):
    result = await ingestion.ingest_kr_disclosures_for_date(
        _db(),
        SATURDAY,
        fetch_rows=AsyncMock(return_value=[]),
        today=SATURDAY,
    )

    assert result.status == "succeeded"
    assert repo.instances[0].partitions[SATURDAY].status == "succeeded"


@pytest.mark.asyncio
async def test_today_with_only_unparseable_rows_is_not_pending(repo):
    result = await ingestion.ingest_kr_disclosures_for_date(
        _db(),
        TODAY,
        fetch_rows=AsyncMock(return_value=[{"rcept_no": ""}]),
        today=TODAY,
    )

    assert result.status == "failed"


@pytest.mark.asyncio
async def test_registry_failure_does_not_fail_dart_ingestion(repo):
    row = {
        "rcept_no": "20261110000001",
        "rcept_dt": "20261110",
        "corp_name": "Synthetic Listed Co",
        "corp_code": "00000001",
        "report_nm": "주요사항보고서",
    }
    result = await ingestion.ingest_kr_disclosures_for_date(
        _db(),
        TODAY,
        fetch_rows=AsyncMock(return_value=[row]),
        today=TODAY,
        load_corp_registry=AsyncMock(return_value=None),
    )

    assert result.status == "succeeded"
    assert result.event_count == 1
    event = repo.instances[-1].events[0]
    assert event["symbol"] is None
    assert event["raw_payload_json"]["symbol_resolution_reason"] == (
        "registry_unavailable"
    )


class _Client:
    def __init__(self):
        self.cache_args: list[bool] = []

    def list_date_ex(self, iso, cache=True):
        self.cache_args.append(cache)
        return pd.DataFrame(
            columns=sorted(dart_helpers.DART_LIST_DATE_EX_REQUIRED_COLUMNS)
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target", "use_cache", "expected_cache"),
    [
        (TODAY, True, False),  # today never hits the persisted page cache
        (date(2026, 11, 11), True, False),
        (YESTERDAY, True, True),  # completed days keep the cache
        (YESTERDAY, False, False),  # explicit opt-out is preserved
    ],
)
async def test_list_date_ex_cache_argument(
    monkeypatch, target, use_cache, expected_cache
):
    client = _Client()
    monkeypatch.setattr(dart_helpers, "_get_client", AsyncMock(return_value=client))
    monkeypatch.setattr(dart_helpers, "today_kst", lambda: TODAY)

    await dart_helpers.fetch_dart_filings_for_date(target, use_cache=use_cache)

    assert client.cache_args == [expected_cache]


@pytest.mark.asyncio
async def test_cli_summary_reports_pending_separately(monkeypatch, capsys):
    from scripts import ingest_market_events as cli

    fake = AsyncMock(
        side_effect=[
            SimpleNamespace(status="succeeded", event_count=3, error=None),
            SimpleNamespace(status="pending", event_count=0, error=None),
        ]
    )
    monkeypatch.setitem(cli.SUPPORTED, ("dart", "disclosure", "kr"), fake)
    db = MagicMock()
    db.commit = AsyncMock()

    rc = await cli.run_ingest(
        db=db,
        source="dart",
        category="disclosure",
        market="kr",
        from_date=YESTERDAY,
        to_date=TODAY,
        dry_run=False,
    )

    summary = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 0
    assert summary["succeeded"] == 1
    assert summary["failed"] == 0
    assert summary["pending"] == 1
