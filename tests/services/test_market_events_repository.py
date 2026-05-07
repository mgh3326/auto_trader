"""DB-backed tests for MarketEventsRepository (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

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


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upsert_event_with_values_inserts_event_and_values(db_session):
    from app.models.market_events import MarketEvent, MarketEventValue
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "earnings",
        "market": "us",
        "country": "US",
        "symbol": "IONQ",
        "title": "IONQ earnings release",
        "event_date": date(2026, 5, 7),
        "time_hint": "after_close",
        "status": "released",
        "source": "finnhub",
        "source_event_id": None,
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
        "raw_payload_json": {"symbol": "IONQ"},
    }
    values = [
        {
            "metric_name": "eps",
            "period": "Q1-2026",
            "actual": Decimal("-0.38"),
            "forecast": Decimal("-0.36"),
            "unit": "USD",
        },
    ]

    event = await repo.upsert_event_with_values(event_dict, values)
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].symbol == "IONQ"
    vrows = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(vrows) == 1
    assert vrows[0].event_id == event.id


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upsert_event_is_idempotent_on_natural_key(db_session):
    from app.models.market_events import MarketEvent, MarketEventValue
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "earnings",
        "market": "us",
        "symbol": "IONQ",
        "title": "IONQ earnings release",
        "event_date": date(2026, 5, 7),
        "status": "scheduled",
        "source": "finnhub",
        "source_event_id": None,
        "fiscal_year": 2026,
        "fiscal_quarter": 1,
    }
    values = [
        {
            "metric_name": "eps",
            "period": "Q1-2026",
            "forecast": Decimal("-0.36"),
            "unit": "USD",
        },
    ]
    await repo.upsert_event_with_values(event_dict, values)
    await db_session.commit()

    # Second call with the same natural key + updated status/value
    event_dict_v2 = {**event_dict, "status": "released"}
    values_v2 = [
        {
            "metric_name": "eps",
            "period": "Q1-2026",
            "actual": Decimal("-0.38"),
            "forecast": Decimal("-0.36"),
            "unit": "USD",
        },
    ]
    await repo.upsert_event_with_values(event_dict_v2, values_v2)
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "released"
    vrows = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(vrows) == 1
    assert vrows[0].actual == Decimal("-0.38")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upsert_event_is_idempotent_when_natural_key_has_nulls(db_session):
    from app.models.market_events import MarketEvent, MarketEventValue
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "earnings",
        "market": "us",
        "symbol": "U",
        "title": "U earnings release",
        "event_date": date(2026, 5, 7),
        "status": "scheduled",
        "source": "finnhub",
        "source_event_id": None,
        "fiscal_year": None,
        "fiscal_quarter": None,
    }
    values = [
        {
            "metric_name": "eps",
            "period": None,
            "forecast": Decimal("-0.52"),
            "unit": "USD",
        },
    ]

    await repo.upsert_event_with_values(event_dict, values)
    await db_session.commit()
    await repo.upsert_event_with_values(
        {**event_dict, "status": "released"},
        [{**values[0], "actual": Decimal("-0.34")}],
    )
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].status == "released"
    vrows = (await db_session.execute(select(MarketEventValue))).scalars().all()
    assert len(vrows) == 1
    assert vrows[0].actual == Decimal("-0.34")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upsert_event_with_source_event_id_uses_id_key(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    event_dict = {
        "category": "disclosure",
        "market": "kr",
        "symbol": "005930",
        "title": "분기보고서",
        "event_date": date(2026, 5, 7),
        "status": "released",
        "source": "dart",
        "source_event_id": "20260507000123",
    }
    await repo.upsert_event_with_values(event_dict, [])
    await db_session.commit()

    # Same source_event_id with updated title
    await repo.upsert_event_with_values(
        {**event_dict, "title": "분기보고서 (2026.03)"}, []
    )
    await db_session.commit()

    rows = (await db_session.execute(select(MarketEvent))).scalars().all()
    assert len(rows) == 1
    assert rows[0].title == "분기보고서 (2026.03)"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_partition_lifecycle_records_running_succeeded(db_session):
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    p = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 7),
    )
    assert p.status == "pending"

    await repo.mark_partition_running(p)
    assert p.status == "running"
    assert p.started_at is not None

    await repo.mark_partition_succeeded(p, event_count=42)
    assert p.status == "succeeded"
    assert p.event_count == 42
    assert p.finished_at is not None
    await db_session.commit()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_partition_failure_increments_retry_count(db_session):
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    p = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 8),
    )
    await repo.mark_partition_failed(p, error="read timeout")
    assert p.status == "failed"
    assert p.retry_count == 1
    assert p.last_error == "read timeout"

    await repo.mark_partition_failed(p, error="another")
    assert p.retry_count == 2
    await db_session.commit()
