"""Ingestion orchestration tests (ROB-128)."""

from __future__ import annotations

from datetime import UTC, date
from datetime import datetime as _dt
from unittest.mock import AsyncMock

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

    await db_session.execute(delete(MarketEventValue))
    await db_session.execute(delete(MarketEvent))
    await db_session.execute(delete(MarketEventIngestionPartition))
    await db_session.commit()
    yield


FINNHUB_RESPONSE_ONE_ROW = {
    "symbol": None,
    "instrument_type": "equity_us",
    "source": "finnhub",
    "from_date": "2026-05-07",
    "to_date": "2026-05-07",
    "count": 1,
    "earnings": [
        {
            "symbol": "IONQ",
            "date": "2026-05-07",
            "hour": "amc",
            "eps_estimate": -0.3593,
            "eps_actual": -0.38,
            "revenue_estimate": 50729332,
            "revenue_actual": 64670000,
            "quarter": 1,
            "year": 2026,
        }
    ],
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_date_succeeds(db_session, monkeypatch):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_ONE_ROW)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    result = await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 7))
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1
    fake.assert_awaited_once_with(None, "2026-05-07", "2026-05-07")

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].symbol == "IONQ"

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "succeeded"
    assert parts[0].event_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_date_records_failure(db_session, monkeypatch):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(side_effect=TimeoutError("read timeout=10"))
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    result = await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 8))
    await db_session.commit()

    assert result.status == "failed"
    assert result.event_count == 0
    assert "read timeout" in (result.error or "")

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_date_is_idempotent(db_session, monkeypatch):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_ONE_ROW)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 7))
    await db_session.commit()
    await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 7))
    await db_session.commit()

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "succeeded"


DART_ROW = {
    "rcept_no": "20260507000123",
    "rcept_dt": "20260507",
    "corp_name": "삼성전자",
    "corp_code": "00126380",
    "stock_code": "005930",
    "report_nm": "분기보고서 (2026.03)",
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_kr_disclosures_for_date_with_injected_fetcher(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        assert d == date(2026, 5, 7)
        return [DART_ROW]

    result = await ingestion.ingest_kr_disclosures_for_date(
        db_session, date(2026, 5, 7), fetch_rows=fake_fetch
    )
    await db_session.commit()
    assert result.status == "succeeded"
    assert result.event_count == 1

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].source == "dart"
    assert rows[0].source_event_id == "20260507000123"
    assert rows[0].symbol == "005930"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_records_failure_when_upsert_fails(
    db_session, monkeypatch
):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_ONE_ROW)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    async def boom(*args, **kwargs):
        raise RuntimeError("database write failed")

    monkeypatch.setattr(
        ingestion.MarketEventsRepository, "upsert_event_with_values", boom
    )

    result = await ingestion.ingest_us_earnings_for_date(db_session, date(2026, 5, 9))
    await db_session.commit()

    assert result.status == "failed"
    assert "database write failed" in (result.error or "")
    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1


FF_ROW = {
    "title": "Core CPI m/m",
    "currency": "USD",
    "country": "USD",
    "event_date": date(2026, 5, 13),
    "release_time_utc": _dt(2026, 5, 13, 12, 30, tzinfo=UTC),
    "release_time_local": _dt(2026, 5, 13, 8, 30),
    "time_hint_raw": "8:30am",
    "impact": "high",
    "actual": "0.3%",
    "forecast": "0.3%",
    "previous": "0.4%",
    "source_event_id": "ff::USD::Core CPI m/m::2026-05-13T12:30:00Z",
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_economic_events_for_date_succeeds(db_session):
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        assert d == date(2026, 5, 13)
        return [FF_ROW]

    result = await ingestion.ingest_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake_fetch
    )
    await db_session.commit()

    assert result.status == "succeeded"
    assert result.event_count == 1

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1
    e = events[0]
    assert e.category == "economic"
    assert e.market == "global"
    assert e.source == "forexfactory"
    assert e.currency == "USD"
    assert e.importance == 3

    values = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(values) == 1
    assert values[0].metric_name == "actual"
    assert values[0].unit == "%"

    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].source == "forexfactory"
    assert parts[0].category == "economic"
    assert parts[0].market == "global"
    assert parts[0].status == "succeeded"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_economic_events_is_idempotent(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    async def fake_fetch(d):
        return [FF_ROW]

    for _ in range(2):
        await ingestion.ingest_economic_events_for_date(
            db_session, date(2026, 5, 13), fetch_rows=fake_fetch
        )
        await db_session.commit()

    events = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_economic_events_records_failure(db_session):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion

    async def boom(d):
        raise TimeoutError("fetch timed out")

    result = await ingestion.ingest_economic_events_for_date(
        db_session, date(2026, 5, 13), fetch_rows=boom
    )
    await db_session.commit()

    assert result.status == "failed"
    parts = (
        (await db_session.execute(select(MarketEventIngestionPartition)))
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1
