"""TradingView ingestion orchestration tests (ROB-210).

All tests use injected fetch_rows — no live network calls.
"""

from __future__ import annotations

from datetime import UTC, date
from datetime import datetime as _dt

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    tradingview_events = select(MarketEvent.id).where(
        MarketEvent.source == "tradingview"
    )
    await db_session.execute(
        delete(MarketEventValue).where(
            MarketEventValue.event_id.in_(tradingview_events)
        )
    )
    await db_session.execute(
        delete(MarketEvent).where(MarketEvent.source == "tradingview")
    )
    await db_session.execute(
        delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.source == "tradingview"
        )
    )
    await db_session.commit()
    yield


TV_ROW = {
    "id": "4b8e4f00-0e49-4a4a-b6e2-111111111111",
    "title": "Core CPI m/m",
    "country": "US",
    "date_utc": _dt(2026, 5, 13, 12, 30, tzinfo=UTC),
    "period": "Apr",
    "actual": "0.3",
    "forecast": "0.3",
    "previous": "0.4",
    "unit": "%",
    "source": "Bureau of Labor Statistics",
    "source_url": "https://www.bls.gov/cpi/",
    "ticker": None,
    "importance": 3,
    "_raw": {
        "id": "4b8e4f00-0e49-4a4a-b6e2-111111111111",
        "title": "Core CPI m/m",
        "country": "US",
        "date": "2026-05-13T12:30:00Z",
        "period": "Apr",
        "actual": "0.3",
        "forecast": "0.3",
        "previous": "0.4",
        "unit": "%",
        "source": "Bureau of Labor Statistics",
        "source_url": "https://www.bls.gov/cpi/",
        "ticker": None,
        "importance": 3,
    },
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_tradingview_economic_events_succeeds(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        assert d == date(2026, 5, 13)
        return [TV_ROW]

    result = await ingestion.ingest_tradingview_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake_fetch
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1
    assert result.source == "tradingview"
    assert result.category == "economic"
    assert result.market == "global"

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "tradingview")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    e = events[0]
    assert e.category == "economic"
    assert e.market == "global"
    assert e.source == "tradingview"
    assert e.country == "US"
    assert e.title == "Core CPI m/m"
    assert e.importance == 3
    assert e.status == "released"
    assert e.source_event_id == "4b8e4f00-0e49-4a4a-b6e2-111111111111"
    assert e.source_url == "https://www.bls.gov/cpi/"

    values = (
        (
            await db_session.execute(
                select(MarketEventValue).where(MarketEventValue.event_id == e.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(values) == 1
    assert values[0].metric_name == "actual"
    assert values[0].unit == "%"

    parts = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == "tradingview"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].source == "tradingview"
    assert parts[0].category == "economic"
    assert parts[0].market == "global"
    assert parts[0].status == "succeeded"
    assert parts[0].event_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_tradingview_economic_events_is_idempotent(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        return [TV_ROW]

    for _ in range(2):
        await ingestion.ingest_tradingview_economic_events_for_date(
            db_session, date(2026, 5, 13), fetch_rows=fake_fetch
        )
        await db_session.commit()

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "tradingview")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_tradingview_economic_events_records_failure(db_session):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion

    async def boom(d):
        raise TimeoutError("tradingview fetch timed out")

    result = await ingestion.ingest_tradingview_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=boom
    )
    await db_session.commit()

    assert result.status == "failed"
    assert "timed out" in (result.error or "")

    parts = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == "tradingview"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_tradingview_skips_unparseable_rows(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    bad_row = {**TV_ROW, "title": ""}  # will raise ValueError in normalizer

    async def fake_fetch(d):
        return [bad_row, TV_ROW]

    result = await ingestion.ingest_tradingview_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake_fetch
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "tradingview")
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_tradingview_empty_fetch_succeeds_with_zero_count(db_session):
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        return []

    result = await ingestion.ingest_tradingview_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake_fetch
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 0
