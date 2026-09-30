"""CLI tests for scripts/backfill_dart_disclosures.py (#1085)."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.db import engine
from tests._run_owned_database import validate_run_owned_database_url
from tests.market_events_test_helpers import market_events_test_lock

validate_run_owned_database_url(engine.url)


@pytest_asyncio.fixture(autouse=True)
async def _market_events_lock():
    async with market_events_test_lock():
        yield


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session, _market_events_lock):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    await db_session.execute(delete(MarketEventValue))
    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


DART_ROW = {
    "rcept_no": "20260507000123",
    "rcept_dt": "20260507",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "stock_code": "005930",
    "report_nm": "분기보고서 (2026.03)",
}

DART_ROW_2 = {
    "rcept_no": "20260507000456",
    "rcept_dt": "20260507",
    "corp_name": "가상상사",
    "corp_code": "99999999",
    "stock_code": "999999",
    "report_nm": "주요사항보고서 (유상증자결정)",
}


@pytest.mark.unit
def test_parse_args_defaults_to_dry_run():
    from scripts.backfill_dart_disclosures import parse_args

    ns = parse_args(["--from-date", "2026-01-01", "--to-date", "2026-01-31"])
    assert ns.from_date == date(2026, 1, 1)
    assert ns.to_date == date(2026, 1, 31)
    assert ns.commit is False


@pytest.mark.unit
def test_parse_args_commit_flag():
    from scripts.backfill_dart_disclosures import parse_args

    ns = parse_args(
        ["--from-date", "2026-01-01", "--to-date", "2026-01-31", "--commit"]
    )
    assert ns.commit is True


@pytest.mark.unit
def test_parse_args_dry_run_and_commit_are_mutually_exclusive():
    from scripts.backfill_dart_disclosures import parse_args

    with pytest.raises(SystemExit):
        parse_args(
            [
                "--from-date",
                "2026-01-01",
                "--to-date",
                "2026-01-31",
                "--dry-run",
                "--commit",
            ]
        )


@pytest.mark.unit
def test_parse_args_requires_from_and_to_date():
    from scripts.backfill_dart_disclosures import parse_args

    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["--from-date", "2026-01-01"])
    with pytest.raises(SystemExit):
        parse_args(["--to-date", "2026-01-31"])


@pytest.mark.unit
def test_parse_args_rejects_reversed_range():
    from scripts.backfill_dart_disclosures import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--from-date", "2026-02-01", "--to-date", "2026-01-31"])


@pytest.mark.asyncio
@pytest.mark.unit
async def test_run_backfill_rejects_reversed_range():
    from scripts.backfill_dart_disclosures import run_backfill

    with pytest.raises(ValueError, match="from_date must be <= to_date"):
        await run_backfill(
            db=None,  # type: ignore[arg-type] - validation precedes any db use
            from_date=date(2026, 2, 1),
            to_date=date(2026, 1, 31),
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dry_run_prints_per_day_counts_and_writes_nothing(db_session):
    """Default dry-run mode fetches and reports but never writes (#1085)."""
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )
    from scripts import backfill_dart_disclosures as cli

    fetch = AsyncMock(return_value=[dict(DART_ROW)])
    lines: list[str] = []

    rc = await cli.run_backfill(
        db=db_session,
        from_date=date(2026, 5, 6),
        to_date=date(2026, 5, 8),
        commit=False,
        fetch_rows=fetch,
        emit=lines.append,
    )

    assert rc == 0
    assert fetch.await_count == 3
    per_day = [line for line in lines if line.startswith("2026-05-")]
    assert len(per_day) == 3
    assert any("rows=1" in line and "parseable=1" in line for line in per_day)
    assert any("session=open" in line for line in per_day)
    summary = json.loads(lines[-1])
    assert summary["dry_run"] is True
    assert summary["succeeded"] == 3
    assert summary["failed"] == 0

    for model in (MarketEvent, MarketEventValue, MarketEventIngestionPartition):
        rows = (await db_session.execute(select(model))).scalars().all()
        assert rows == [], f"dry-run wrote {model.__tablename__} rows"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dry_run_reports_existing_partition_state(db_session):
    """Dry-run shows the current partition status so wrongly-succeeded days
    are visible before the desk commits."""
    from app.models.market_events import MarketEventIngestionPartition
    from scripts import backfill_dart_disclosures as cli

    db_session.add(
        MarketEventIngestionPartition(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=date(2026, 5, 7),
            status="succeeded",
            event_count=0,
        )
    )
    await db_session.commit()

    lines: list[str] = []
    rc = await cli.run_backfill(
        db=db_session,
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        commit=False,
        fetch_rows=AsyncMock(return_value=[dict(DART_ROW)]),
        emit=lines.append,
    )

    assert rc == 0
    assert any("partition=succeeded" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dry_run_predicts_failure_on_zero_filings_trading_day(db_session):
    from scripts import backfill_dart_disclosures as cli

    lines: list[str] = []
    rc = await cli.run_backfill(
        db=db_session,
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 7),
        commit=False,
        fetch_rows=AsyncMock(return_value=[]),
        emit=lines.append,
    )

    assert rc == 2
    assert any("action=fail_zero_filings_on_trading_day" in line for line in lines)
    summary = json.loads(lines[-1])
    assert summary["failed"] == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_dry_run_accepts_zero_on_holiday_and_weekend(db_session):
    """2026-05-09 Sat + 2026-08-17 substitute holiday are XKRX non-sessions."""
    from scripts import backfill_dart_disclosures as cli

    lines: list[str] = []
    rc = await cli.run_backfill(
        db=db_session,
        from_date=date(2026, 5, 9),
        to_date=date(2026, 5, 9),
        commit=False,
        fetch_rows=AsyncMock(return_value=[]),
        emit=lines.append,
    )
    assert rc == 0
    assert any("action=ok_zero_on_closed_day" in line for line in lines)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_commit_writes_through_ingestion_path(db_session):
    """--commit routes each day through ingest_kr_disclosures_for_date."""
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from scripts import backfill_dart_disclosures as cli

    trading_day = date(2026, 5, 7)

    async def fake_fetch(d):
        assert d == trading_day
        return [dict(DART_ROW), dict(DART_ROW_2)]

    lines: list[str] = []
    rc = await cli.run_backfill(
        db=db_session,
        from_date=trading_day,
        to_date=trading_day,
        commit=True,
        fetch_rows=fake_fetch,
        emit=lines.append,
    )

    assert rc == 0
    assert any("status=succeeded" in line and "events=2" in line for line in lines)
    summary = json.loads(lines[-1])
    assert summary["dry_run"] is False
    assert summary["succeeded"] == 1

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "dart")
            )
        )
        .scalars()
        .all()
    )
    assert {e.source_event_id for e in events} == {
        DART_ROW["rcept_no"],
        DART_ROW_2["rcept_no"],
    }

    partitions = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == "dart"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(partitions) == 1
    assert partitions[0].status == "succeeded"
    assert partitions[0].event_count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_commit_is_idempotent_by_rcept_no(db_session):
    """Running the backfill twice upserts — no duplicate events (#1085)."""
    from app.models.market_events import MarketEvent
    from scripts import backfill_dart_disclosures as cli

    trading_day = date(2026, 5, 7)

    async def fake_fetch(d):
        return [dict(DART_ROW), dict(DART_ROW_2)]

    for _ in range(2):
        rc = await cli.run_backfill(
            db=db_session,
            from_date=trading_day,
            to_date=trading_day,
            commit=True,
            fetch_rows=fake_fetch,
            emit=lambda _line: None,
        )
        assert rc == 0

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "dart")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_commit_marks_zero_filings_trading_day_failed_and_continues(
    db_session,
):
    """A trading day with zero filings records `failed`, not `succeeded`, and
    the run still processes subsequent days (#1085)."""
    from app.models.market_events import MarketEventIngestionPartition
    from scripts import backfill_dart_disclosures as cli

    calls: list[date] = []

    async def fake_fetch(d):
        calls.append(d)
        if d == date(2026, 5, 7):
            return []
        return [dict(DART_ROW_2)]

    lines: list[str] = []
    rc = await cli.run_backfill(
        db=db_session,
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 8),
        commit=True,
        fetch_rows=fake_fetch,
        emit=lines.append,
    )

    assert rc == 2
    assert calls == [date(2026, 5, 7), date(2026, 5, 8)]
    assert any("status=failed" in line for line in lines if "2026-05-07" in line)
    assert any("status=succeeded" in line for line in lines if "2026-05-08" in line)
    summary = json.loads(lines[-1])
    assert summary["succeeded"] == 1
    assert summary["failed"] == 1

    partitions = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition)
                .where(MarketEventIngestionPartition.source == "dart")
                .order_by(MarketEventIngestionPartition.partition_date)
            )
        )
        .scalars()
        .all()
    )
    assert len(partitions) == 2
    assert partitions[0].status == "failed"
    assert "confirmed XKRX trading session" in (partitions[0].last_error or "")
    assert partitions[1].status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_commit_repairs_wrongly_succeeded_zero_partition(db_session):
    """The #1054 repair path: a partition marked succeeded with 0 events on a
    trading day is re-ingested and gets the real rows (#1085)."""
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from scripts import backfill_dart_disclosures as cli

    trading_day = date(2026, 5, 7)
    db_session.add(
        MarketEventIngestionPartition(
            source="dart",
            category="disclosure",
            market="kr",
            partition_date=trading_day,
            status="succeeded",
            event_count=0,
        )
    )
    await db_session.commit()

    async def fake_fetch(d):
        return [dict(DART_ROW)]

    rc = await cli.run_backfill(
        db=db_session,
        from_date=trading_day,
        to_date=trading_day,
        commit=True,
        fetch_rows=fake_fetch,
        emit=lambda _line: None,
    )
    assert rc == 0

    partition = (
        await db_session.execute(
            select(MarketEventIngestionPartition).where(
                MarketEventIngestionPartition.source == "dart"
            )
        )
    ).scalar_one()
    assert partition.status == "succeeded"
    assert partition.event_count == 1

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "dart")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_main_opens_session_and_runs_dry_run_by_default(db_session, monkeypatch):
    """main() defaults to dry-run: run_backfill receives commit=False."""
    from scripts import backfill_dart_disclosures as cli

    captured: dict[str, object] = {}

    async def fake_run_backfill(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli, "run_backfill", fake_run_backfill)
    rc = await cli.main(["--from-date", "2026-01-01", "--to-date", "2026-01-02"])

    assert rc == 0
    assert captured["commit"] is False
    assert captured["from_date"] == date(2026, 1, 1)
    assert captured["to_date"] == date(2026, 1, 2)
