"""Ingestion orchestration tests (ROB-128)."""

from __future__ import annotations

from datetime import date
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
    monkeypatch.setattr(ingestion, "_fetch_earnings_calendar_finnhub", fake)

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
    monkeypatch.setattr(ingestion, "_fetch_earnings_calendar_finnhub", fake)

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
    monkeypatch.setattr(ingestion, "_fetch_earnings_calendar_finnhub", fake)

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
