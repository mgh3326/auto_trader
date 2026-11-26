"""Push captures are the freshness evidence for Benzinga Pro breaking news."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.models.market_events import MarketEventIngestionPartition
from app.services.market_events.freshness_service import MarketEventsFreshnessService
from tests.services.retro_v1.support import FakeSession, ns

pytestmark = pytest.mark.unit

CAPTURED_AT = datetime(2026, 11, 26, 14, 0, tzinfo=UTC)


def _capture_run(
    *,
    surface: str,
    status: str,
    row_count: int,
    run_id: uuid.UUID,
    reason: str | None = None,
):
    return ns(
        run_id=run_id,
        snapshot_at=CAPTURED_AT,
        trading_date=date(2026, 11, 26),
        surface=surface,
        status=status,
        reason=reason or ("no_rows" if status == "empty" else None),
        row_count=row_count,
    )


@pytest.mark.asyncio
async def test_recent_push_capture_with_breaking_news_is_fresh() -> None:
    run_id = uuid.uuid4()
    session = FakeSession(
        {
            "FROM market_event_ingestion_partitions": [],
            "FROM market_events": [("benzinga_pro", "breaking_news", "us", 2)],
            "FROM invest_momentum_event_runs": [
                _capture_run(
                    surface=f"us:benzinga_pro:us_regular:{side}",
                    status="ok",
                    row_count=1,
                    run_id=run_id,
                )
                for side in ("up", "down")
            ],
        }
    )

    response = await MarketEventsFreshnessService(session).compute(
        window_from=CAPTURED_AT.date(),
        window_to=CAPTURED_AT.date(),
        now=CAPTURED_AT,
    )

    row = next(
        item
        for item in response.rows
        if (item.source, item.category, item.market)
        == ("benzinga_pro", "breaking_news", "us")
    )
    assert row.stale is False
    assert row.event_count_in_window == 2
    assert row.partition_count_succeeded == 1


@pytest.mark.asyncio
async def test_recent_empty_push_capture_is_not_reported_stale() -> None:
    run_id = uuid.uuid4()
    session = FakeSession(
        {
            "FROM market_event_ingestion_partitions": [],
            "FROM market_events": [],
            "FROM invest_momentum_event_runs": [
                _capture_run(
                    surface=f"us:benzinga_pro:us_regular:{side}",
                    status="empty",
                    row_count=0,
                    run_id=run_id,
                )
                for side in ("up", "down")
            ],
        }
    )

    response = await MarketEventsFreshnessService(session).compute(
        window_from=CAPTURED_AT.date(),
        window_to=CAPTURED_AT.date(),
        now=CAPTURED_AT,
    )

    row = next(item for item in response.rows if item.source == "benzinga_pro")
    assert row.stale is False
    assert row.event_count_in_window == 0
    assert row.partition_count_succeeded == 1


@pytest.mark.asyncio
async def test_no_push_capture_is_stale_lag() -> None:
    session = FakeSession(
        {
            "FROM market_event_ingestion_partitions": [],
            "FROM market_events": [],
            "FROM invest_momentum_event_runs": [],
        }
    )

    response = await MarketEventsFreshnessService(session).compute(
        window_from=CAPTURED_AT.date(),
        window_to=CAPTURED_AT.date(),
        now=CAPTURED_AT,
    )

    row = next(item for item in response.rows if item.source == "benzinga_pro")
    assert row.stale is True
    assert row.partition_count_total == 0
    assert row.latest_succeeded_finished_at is None


@pytest.mark.asyncio
async def test_push_capture_error_is_counted_as_failed_evidence() -> None:
    run_id = uuid.uuid4()
    session = FakeSession(
        {
            "FROM market_event_ingestion_partitions": [],
            "FROM market_events": [],
            "FROM invest_momentum_event_runs": [
                _capture_run(
                    surface=f"us:benzinga_pro:us_regular:{side}",
                    status="error",
                    row_count=0,
                    run_id=run_id,
                    reason="timeout",
                )
                for side in ("up", "down")
            ],
        }
    )

    response = await MarketEventsFreshnessService(session).compute(
        window_from=CAPTURED_AT.date(),
        window_to=CAPTURED_AT.date(),
        now=CAPTURED_AT,
    )

    row = next(item for item in response.rows if item.source == "benzinga_pro")
    assert row.partition_count_failed == 1
    assert row.latest_failed_error == "timeout"


@pytest.mark.asyncio
async def test_pull_source_freshness_projection_remains_byte_identical() -> None:
    finished_at = CAPTURED_AT - timedelta(hours=2)
    partition = MarketEventIngestionPartition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=CAPTURED_AT.date(),
        status="succeeded",
        event_count=0,
        finished_at=finished_at,
        last_error=None,
        retry_count=0,
    )
    session = FakeSession(
        {
            "FROM market_event_ingestion_partitions": [partition],
            "FROM market_events": [("finnhub", "earnings", "us", 1)],
            "FROM invest_momentum_event_runs": [],
        }
    )

    response = await MarketEventsFreshnessService(session).compute(
        window_from=CAPTURED_AT.date(),
        window_to=CAPTURED_AT.date(),
        now=CAPTURED_AT,
    )
    row = next(item for item in response.rows if item.source == "finnhub")

    assert row.model_dump_json() == (
        '{"source":"finnhub","category":"earnings","market":"us",'
        '"window_from":"2026-11-26","window_to":"2026-11-26",'
        '"partition_count_total":1,"partition_count_succeeded":1,'
        '"partition_count_failed":0,"partition_count_running":0,'
        '"partition_count_pending":0,"partition_count_missing":0,'
        '"event_count_in_window":1,'
        '"latest_succeeded_partition_date":"2026-11-26",'
        '"latest_succeeded_finished_at":"2026-11-26T12:00:00Z",'
        '"hours_since_latest_succeeded":2.0,'
        '"latest_failed_partition_date":null,"latest_failed_error":null,'
        '"expected_next_refresh_at":null,"stale":false}'
    )
