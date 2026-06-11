"""WiseFn KR earnings ingestion orchestrator tests (ROB-171)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import delete, select


@pytest_asyncio.fixture(autouse=True)
async def _clean_market_events(db_session):
    # Scoped to source="wisefn" (mirrors test_tradingview_ingestion.py): xdist
    # --dist=loadfile runs other market_events test FILES concurrently against
    # the same shared Postgres, so a blanket delete(MarketEvent) here races
    # their commit-then-select windows (observed: tradingview's len(events)==1
    # flipping to 0 after a PR re-shard). Every assertion in this file already
    # filters source=="wisefn".
    from app.models.market_events import (
        MarketEvent,
        MarketEventIngestionPartition,
        MarketEventValue,
    )

    wisefn_events = select(MarketEvent.id).where(MarketEvent.source == "wisefn")
    await db_session.execute(
        delete(MarketEventValue).where(MarketEventValue.event_id.in_(wisefn_events))
    )
    await db_session.execute(delete(MarketEvent).where(MarketEvent.source == "wisefn"))
    await db_session.execute(
        delete(MarketEventIngestionPartition).where(
            MarketEventIngestionPartition.source == "wisefn"
        )
    )
    await db_session.commit()
    yield


WISEFN_ROW = {
    "stock_code": "005930",
    "corp_name": "삼성전자",
    "release_date": "2026-05-13",
    "fiscal_year": 2026,
    "fiscal_quarter": 1,
    "release_type": "scheduled",
    "title": "삼성전자 2026년 1분기 실적발표 예정",
    "time_hint": "after_close",
}


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_succeeds_with_injected_rows(db_session):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=[WISEFN_ROW])
    result = await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake
    )
    await db_session.flush()

    assert result.status == "succeeded"
    assert result.event_count == 1
    fake.assert_awaited_once_with(date(2026, 5, 13))

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(
                    MarketEvent.source == "wisefn",
                    MarketEvent.category == "earnings",
                    MarketEvent.market == "kr",
                    MarketEvent.source_event_id
                    == "wisefn::005930::2026-05-13::2026::1",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].symbol == "005930"
    assert events[0].source == "wisefn"
    assert events[0].category == "earnings"
    assert events[0].market == "kr"
    assert events[0].source_event_id == "wisefn::005930::2026-05-13::2026::1"

    parts = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == "wisefn",
                    MarketEventIngestionPartition.category == "earnings",
                    MarketEventIngestionPartition.market == "kr",
                    MarketEventIngestionPartition.partition_date == date(2026, 5, 13),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(parts) == 1
    assert parts[0].source == "wisefn"
    assert parts[0].status == "succeeded"
    assert parts[0].event_count == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_is_idempotent_on_repeat(db_session):
    from app.models.market_events import MarketEvent
    from app.services.market_events import ingestion

    fake = AsyncMock(return_value=[WISEFN_ROW])
    await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake
    )
    await db_session.flush()
    await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=fake
    )
    await db_session.flush()

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(
                    MarketEvent.source == "wisefn",
                    MarketEvent.category == "earnings",
                    MarketEvent.market == "kr",
                    MarketEvent.source_event_id
                    == "wisefn::005930::2026-05-13::2026::1",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1, "repeat ingestion must upsert, not duplicate"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_marks_failed_on_fetch_error(db_session):
    from app.models.market_events import MarketEvent, MarketEventIngestionPartition
    from app.services.market_events import ingestion

    async def boom(_d):
        raise NotImplementedError("contract not wired")

    result = await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=boom
    )
    await db_session.flush()

    assert result.status == "failed"
    assert "NotImplementedError" in (result.error or "") or "contract" in (
        result.error or ""
    )

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(
                    MarketEvent.source == "wisefn",
                    MarketEvent.category == "earnings",
                    MarketEvent.market == "kr",
                    MarketEvent.event_date == date(2026, 5, 13),
                )
            )
        )
        .scalars()
        .all()
    )
    assert events == []
    parts = (
        (
            await db_session.execute(
                select(MarketEventIngestionPartition).where(
                    MarketEventIngestionPartition.source == "wisefn",
                    MarketEventIngestionPartition.category == "earnings",
                    MarketEventIngestionPartition.market == "kr",
                    MarketEventIngestionPartition.partition_date == date(2026, 5, 13),
                )
            )
        )
        .scalars()
        .all()
    )
    assert parts[0].status == "failed"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_ingest_wisefn_default_fetch_uses_helper(db_session, monkeypatch):
    """When fetch_rows is None, the orchestrator wires fetch_wisefn_earnings_for_date."""
    from app.services.market_events import ingestion, wisefn_helpers

    captured = {}

    async def stub(target_date):
        captured["called"] = target_date
        return []

    monkeypatch.setattr(wisefn_helpers, "fetch_wisefn_earnings_for_date", stub)

    result = await ingestion.ingest_kr_earnings_wisefn_for_date(
        db_session, date(2026, 5, 13), fetch_rows=None
    )
    await db_session.flush()

    assert captured == {"called": date(2026, 5, 13)}
    assert result.status == "succeeded"
    assert result.event_count == 0
