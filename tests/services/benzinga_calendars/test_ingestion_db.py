"""CI-only PostgreSQL round trip for Benzinga calendar ingestion (hk 6513)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select

from app.models.market_events import (
    MarketEvent,
    MarketEventRevision,
)
from app.services.benzinga_calendars.ingestion import (
    ingest_benzinga_category_for_range,
)
from app.services.market_events.repository import MarketEventsRepository

NOW = datetime(2026, 11, 25, 14, 0, tzinfo=UTC)

EARNINGS_ROW = {
    "currency": "USD",
    "date": "2026-10-29",
    "date_confirmed": 1,
    "eps": "2.18",
    "eps_est": "2.10",
    "eps_prior": "1.88",
    "exchange": "NASDAQ",
    "id": "69030cfb619d3a00015b72a3",
    "importance": 5,
    "name": "Apple",
    "period": "Q4",
    "period_year": 2026,
    "revenue": "89537000000",
    "revenue_est": "88500000000",
    "revenue_prior": "81800000000",
    "ticker": "AAPL",
    "time": "amc",
    "updated": 1766187011,
}


class _StubClient:
    def __init__(self, rows):
        self._rows = rows

    async def fetch(self, category, *, from_date, to_date, symbols=None):
        return list(self._rows)


async def _ingest(db, rows, *, day: date = date(2026, 10, 29)):
    return await ingest_benzinga_category_for_range(
        None,
        "earnings",
        day,
        day,
        client=_StubClient(rows),
        repo=MarketEventsRepository(db),
        fetched_at=NOW,
        skip_succeeded=False,
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_reingest_is_idempotent_and_a_revision_is_kept(db_session):
    first = await _ingest(db_session, [EARNINGS_ROW])
    assert [r.status for r in first] == ["succeeded"]

    events = (
        (
            await db_session.execute(
                select(MarketEvent).where(
                    MarketEvent.source == "benzinga",
                    MarketEvent.category == "earnings",
                    MarketEvent.symbol == "AAPL",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    event = events[0]
    assert event.first_received_at == NOW
    assert event.provider_updated_raw == "1766187011"
    assert event.provider_updated_at == datetime.fromtimestamp(1766187011, UTC)

    revisions = (
        (
            await db_session.execute(
                select(MarketEventRevision).where(
                    MarketEventRevision.source_event_id
                    == "benzinga:earnings:69030cfb619d3a00015b72a3"
                )
            )
        )
        .scalars()
        .all()
    )
    assert [row.revision_seq for row in revisions] == [1]

    # Same payload again: no new event row, no new revision, and our first
    # observation clock is not rewritten.
    await _ingest(db_session, [EARNINGS_ROW])
    assert (
        await db_session.execute(
            select(func.count())
            .select_from(MarketEvent)
            .where(MarketEvent.source == "benzinga")
        )
    ).scalar_one() == 1
    assert (
        await db_session.execute(select(func.count()).select_from(MarketEventRevision))
    ).scalar_one() == 1

    # A revised record becomes a second revision; the current row is updated
    # (nothing is overwritten silently).
    revised = {**EARNINGS_ROW, "updated": 1766187099, "eps": "2.25"}
    await _ingest(db_session, [revised])
    revisions = (
        (
            await db_session.execute(
                select(MarketEventRevision).order_by(MarketEventRevision.revision_seq)
            )
        )
        .scalars()
        .all()
    )
    assert [row.revision_seq for row in revisions] == [1, 2]
    assert revisions[0].raw_payload_json["eps"] == "2.18"
    assert revisions[1].raw_payload_json["eps"] == "2.25"

    await db_session.refresh(event)
    assert event.raw_payload_json["eps"] == "2.25"
    assert event.first_received_at == NOW
