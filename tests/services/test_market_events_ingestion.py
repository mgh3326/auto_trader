"""Ingestion orchestration tests (ROB-128)."""

from __future__ import annotations

from datetime import UTC, date
from datetime import datetime as _dt
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from tests.market_events_test_helpers import market_events_test_lock


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


async def _load_events(
    db_session,
    model,
    *,
    source,
    category,
    market,
    event_date=None,
    event_dates=None,
    symbol=None,
    symbols=None,
    source_event_id=None,
):
    stmt = select(model).where(
        model.source == source,
        model.category == category,
        model.market == market,
    )
    if event_date is not None:
        stmt = stmt.where(model.event_date == event_date)
    if event_dates is not None:
        stmt = stmt.where(model.event_date.in_(tuple(event_dates)))
    if symbol is not None:
        stmt = stmt.where(model.symbol == symbol)
    if symbols is not None:
        stmt = stmt.where(model.symbol.in_(tuple(symbols)))
    if source_event_id is not None:
        stmt = stmt.where(model.source_event_id == source_event_id)
    stmt = stmt.order_by(model.event_date, model.symbol)
    return (await db_session.execute(stmt)).scalars().all()


async def _load_partitions(
    db_session,
    model,
    *,
    source,
    category,
    market,
    partition_date=None,
    partition_dates=None,
):
    stmt = select(model).where(
        model.source == source,
        model.category == category,
        model.market == market,
    )
    if partition_date is not None:
        stmt = stmt.where(model.partition_date == partition_date)
    if partition_dates is not None:
        stmt = stmt.where(model.partition_date.in_(tuple(partition_dates)))
    stmt = stmt.order_by(model.partition_date)
    return (await db_session.execute(stmt)).scalars().all()


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

    events = await _load_events(
        db_session,
        MarketEvent,
        source="finnhub",
        category="earnings",
        market="us",
        event_date=date(2026, 5, 7),
        symbol="IONQ",
    )
    assert len(events) == 1
    assert events[0].symbol == "IONQ"

    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 7),
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

    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 8),
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

    db_session.add(
        MarketEvent(
            category="earnings",
            market="us",
            symbol="AAPL",
            event_date=date(2026, 5, 7),
            source="finnhub",
            source_event_id="unrelated::AAPL::2026-05-07",
        )
    )
    await db_session.commit()

    events = await _load_events(
        db_session,
        MarketEvent,
        source="finnhub",
        category="earnings",
        market="us",
        event_date=date(2026, 5, 7),
        symbol="IONQ",
    )
    assert len(events) == 1
    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 7),
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

    rows = await _load_events(
        db_session,
        MarketEvent,
        source="dart",
        category="earnings",
        market="kr",
        event_date=date(2026, 5, 7),
        symbol="005930",
        source_event_id="20260507000123",
    )
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
    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 9),
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

    events = await _load_events(
        db_session,
        MarketEvent,
        source="forexfactory",
        category="economic",
        market="global",
        event_date=date(2026, 5, 13),
        source_event_id=FF_ROW["source_event_id"],
    )
    assert len(events) == 1
    e = events[0]
    assert e.category == "economic"
    assert e.market == "global"
    assert e.source == "forexfactory"
    assert e.currency == "USD"
    assert e.importance == 3

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

    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="forexfactory",
        category="economic",
        market="global",
        partition_date=date(2026, 5, 13),
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

    events = await _load_events(
        db_session,
        MarketEvent,
        source="forexfactory",
        category="economic",
        market="global",
        event_date=date(2026, 5, 13),
        source_event_id=FF_ROW["source_event_id"],
    )
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
    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="forexfactory",
        category="economic",
        market="global",
        partition_date=date(2026, 5, 13),
    )
    assert len(parts) == 1
    assert parts[0].status == "failed"
    assert parts[0].retry_count == 1


# ---------------------------------------------------------------------------
# ROB-184: out-of-window + typed error reasons in ingestion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.integration
async def test_economic_ingestion_marks_failed_out_of_rolling_window(db_session):
    from app.services.market_events.ingestion import (
        ingest_economic_events_for_date,
    )

    async def returns_none(_target_date):
        return None

    result = await ingest_economic_events_for_date(
        db_session,
        date(2026, 4, 1),  # arbitrary past date
        fetch_rows=returns_none,
    )
    assert result.status == "failed"
    assert result.error == "forexfactory_out_of_rolling_window"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_economic_ingestion_marks_failed_rate_limited(db_session):
    from app.services.market_events.forexfactory_helpers import (
        ForexFactoryFetchError,
    )
    from app.services.market_events.ingestion import (
        ingest_economic_events_for_date,
    )

    async def raises_rate_limited(_target_date):
        raise ForexFactoryFetchError("rate_limited")

    result = await ingest_economic_events_for_date(
        db_session,
        date(2026, 5, 13),
        fetch_rows=raises_rate_limited,
    )
    assert result.status == "failed"
    assert result.error == "forexfactory_rate_limited"


# ---------------------------------------------------------------------------
# ROB-264: range-aware US earnings ingestion
# ---------------------------------------------------------------------------


FINNHUB_RESPONSE_RANGE_MULTI_DATE = {
    "symbol": None,
    "instrument_type": "equity_us",
    "source": "finnhub",
    "from_date": "2026-05-11",
    "to_date": "2026-05-13",
    "count": 3,
    "earnings": [
        {
            "symbol": "AAA",
            "date": "2026-05-11",
            "hour": "bmo",
            "eps_estimate": 1.0,
            "eps_actual": None,
            "revenue_estimate": 100,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
        },
        {
            "symbol": "BBB",
            "date": "2026-05-11",
            "hour": "amc",
            "eps_estimate": 2.0,
            "eps_actual": None,
            "revenue_estimate": 200,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
        },
        {
            "symbol": "CCC",
            "date": "2026-05-13",
            "hour": "amc",
            "eps_estimate": 3.0,
            "eps_actual": None,
            "revenue_estimate": 300,
            "revenue_actual": None,
            "quarter": 1,
            "year": 2026,
        },
    ],
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_groups_by_date(db_session, monkeypatch):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_RANGE_MULTI_DATE)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session, date(2026, 5, 11), date(2026, 5, 13)
    )
    await db_session.commit()

    fake.assert_awaited_once_with(None, "2026-05-11", "2026-05-13")

    assert [r.partition_date for r in results] == [
        date(2026, 5, 11),
        date(2026, 5, 12),
        date(2026, 5, 13),
    ]
    assert [r.status for r in results] == ["succeeded", "succeeded", "succeeded"]
    assert [r.event_count for r in results] == [2, 0, 1]

    events = await _load_events(
        db_session,
        MarketEvent,
        source="finnhub",
        category="earnings",
        market="us",
        event_dates=(date(2026, 5, 11), date(2026, 5, 13)),
        symbols=("AAA", "BBB", "CCC"),
    )
    assert sorted(e.symbol for e in events) == ["AAA", "BBB", "CCC"]

    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_dates=(date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)),
    )
    parts_by_date = {p.partition_date: p for p in parts}
    assert set(parts_by_date.keys()) == {
        date(2026, 5, 11),
        date(2026, 5, 12),
        date(2026, 5, 13),
    }
    assert parts_by_date[date(2026, 5, 12)].status == "succeeded"
    assert parts_by_date[date(2026, 5, 12)].event_count == 0


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_skips_succeeded_by_default(
    db_session, monkeypatch
):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    for d in (date(2026, 5, 11), date(2026, 5, 12)):
        p = await repo.get_or_create_partition(
            source="finnhub", category="earnings", market="us", partition_date=d
        )
        await repo.mark_partition_succeeded(p, event_count=1)
    await db_session.commit()

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_RANGE_MULTI_DATE)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session, date(2026, 5, 11), date(2026, 5, 13)
    )
    await db_session.commit()

    fake.assert_awaited_once_with(None, "2026-05-13", "2026-05-13")
    assert [r.partition_date for r in results] == [date(2026, 5, 13)]
    assert results[0].status == "succeeded"
    assert results[0].event_count == 1

    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_dates=(date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)),
    )
    parts_by_date = {p.partition_date: p for p in parts}
    assert parts_by_date[date(2026, 5, 11)].event_count == 1
    assert parts_by_date[date(2026, 5, 12)].event_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_force_replays_succeeded(
    db_session, monkeypatch
):
    from app.services.market_events import ingestion
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    p = await repo.get_or_create_partition(
        source="finnhub",
        category="earnings",
        market="us",
        partition_date=date(2026, 5, 11),
    )
    await repo.mark_partition_succeeded(p, event_count=99)
    await db_session.commit()

    fake = AsyncMock(return_value=FINNHUB_RESPONSE_RANGE_MULTI_DATE)
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session,
        date(2026, 5, 11),
        date(2026, 5, 13),
        skip_succeeded=False,
    )
    await db_session.commit()

    assert [r.partition_date for r in results] == [
        date(2026, 5, 11),
        date(2026, 5, 12),
        date(2026, 5, 13),
    ]
    assert results[0].event_count == 2


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_all_succeeded_skips_fetch(
    db_session, monkeypatch
):
    from app.services.market_events import ingestion
    from app.services.market_events.repository import MarketEventsRepository

    repo = MarketEventsRepository(db_session)
    for d in (date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)):
        p = await repo.get_or_create_partition(
            source="finnhub", category="earnings", market="us", partition_date=d
        )
        await repo.mark_partition_succeeded(p, event_count=0)
    await db_session.commit()

    fake = AsyncMock()
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    results = await ingestion.ingest_us_earnings_for_range(
        db_session, date(2026, 5, 11), date(2026, 5, 13)
    )

    fake.assert_not_awaited()
    assert results == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_us_earnings_for_range_429_is_fail_closed(db_session, monkeypatch):
    from app.models.market_events import MarketEventIngestionPartition
    from app.services.market_events import ingestion
    from app.services.market_events.finnhub_helpers import (
        FinnhubQuotaExceededError,
    )

    fake = AsyncMock(side_effect=FinnhubQuotaExceededError("limit reached"))
    monkeypatch.setattr(ingestion, "fetch_earnings_calendar_finnhub", fake)

    with pytest.raises(FinnhubQuotaExceededError):
        await ingestion.ingest_us_earnings_for_range(
            db_session, date(2026, 5, 11), date(2026, 5, 13)
        )
    await db_session.rollback()

    parts = await _load_partitions(
        db_session,
        MarketEventIngestionPartition,
        source="finnhub",
        category="earnings",
        market="us",
        partition_dates=(date(2026, 5, 11), date(2026, 5, 12), date(2026, 5, 13)),
    )
    assert parts == []
