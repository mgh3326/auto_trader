"""CI-only PostgreSQL round trip for Benzinga calendar ingestion (hk 6513)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text

from app.models.market_events import (
    MarketEvent,
    MarketEventRevision,
    MarketEventValue,
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


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revised_event_date_moves_the_current_row(db_session):
    """A provider date revision must move market_events.event_date.

    ``source_event_id`` identifies the record, so the date is a mutable
    attribute of it: the row is served from the date the provider now says,
    and the previous date survives only in the revision log.
    """
    await _ingest(db_session, [EARNINGS_ROW], day=date(2026, 10, 29))

    moved = {**EARNINGS_ROW, "date": "2026-10-31", "updated": 1766187300}
    await _ingest(db_session, [moved], day=date(2026, 10, 31))

    rows = (
        (
            await db_session.execute(
                select(MarketEvent).where(MarketEvent.source == "benzinga")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].event_date.isoformat() == "2026-10-31"

    revisions = (
        (
            await db_session.execute(
                select(MarketEventRevision).order_by(MarketEventRevision.revision_seq)
            )
        )
        .scalars()
        .all()
    )
    assert [row.raw_payload_json["date"] for row in revisions] == [
        "2026-10-29",
        "2026-10-31",
    ]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_log_rejects_update_and_delete(db_session):
    """Append-only is a DB property, not just a missing grant.

    Each statement runs inside its own savepoint so the rejection leaves the
    test transaction usable (the row is uncommitted; the fixture rolls back).
    """
    await _ingest(db_session, [EARNINGS_ROW])

    for statement in (
        "UPDATE market_event_revisions SET revision_seq = 99",
        "DELETE FROM market_event_revisions",
    ):
        with pytest.raises(Exception) as rejected:
            async with db_session.begin_nested():
                await db_session.execute(text(statement))
        assert "append-only" in str(rejected.value)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_revision_log_records_a_revert_as_a_third_revision(db_session):
    """A → B → A is three revisions, and the newest one describes A.

    With a content_hash unique key the third observation could not be written,
    so the newest revision said B while the row held A.
    """
    await _ingest(db_session, [EARNINGS_ROW], day=date(2026, 10, 29))
    changed = {**EARNINGS_ROW, "updated": 1766187099, "eps": "2.25"}
    await _ingest(db_session, [changed], day=date(2026, 10, 29))
    await _ingest(db_session, [EARNINGS_ROW], day=date(2026, 10, 29))

    revisions = (
        (
            await db_session.execute(
                select(MarketEventRevision).order_by(MarketEventRevision.revision_seq)
            )
        )
        .scalars()
        .all()
    )
    assert [row.revision_seq for row in revisions] == [1, 2, 3]
    assert revisions[-1].raw_payload_json["eps"] == "2.18"

    row = (
        await db_session.execute(
            select(MarketEvent).where(MarketEvent.source == "benzinga")
        )
    ).scalar_one()
    assert row.raw_payload_json["eps"] == "2.18"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_provider_keyed_reingest_never_clears_a_resolved_symbol(db_session):
    """An unresolved re-upsert must not erase what an earlier run resolved.

    DART re-upserts the same ``rcept_no`` every day and emits ``symbol=None``
    when the corp-code registry read fails; the stored symbol has to survive.
    A genuinely moved value still updates.
    """
    repo = MarketEventsRepository(db_session)
    base = {
        "category": "disclosure",
        "market": "kr",
        "country": "KR",
        "symbol": "005930",
        "company_name": "삼성전자",
        "title": "분기보고서",
        "event_date": date(2026, 5, 13),
        "time_hint": "unknown",
        "status": "released",
        "source": "dart",
        "source_event_id": "20260513000001",
    }

    await repo.upsert_event_with_values(dict(base), [])
    await repo.upsert_event_with_values({**base, "symbol": None}, [])

    stmt = select(MarketEvent).where(MarketEvent.source_event_id == "20260513000001")
    row = (await db_session.execute(stmt)).scalar_one()
    assert row.symbol == "005930"

    moved = {**base, "symbol": "000660", "event_date": date(2026, 5, 14)}
    await repo.upsert_event_with_values(moved, [])
    await db_session.refresh(row)
    assert row.symbol == "000660"
    assert row.event_date == date(2026, 5, 14)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_period_revision_replaces_the_superseded_values(db_session):
    """A revision that moves the fiscal period must not keep the old rows.

    ``period`` is part of the value key, so the superseded period's EPS would
    otherwise sit beside the new one and a first-match reader could serve the
    stale number.
    """
    await _ingest(db_session, [EARNINGS_ROW])

    moved = {
        **EARNINGS_ROW,
        "updated": 1766188000,
        "period": "Q1",
        "period_year": 2027,
        "eps": "2.30",
        "revenue": "95000000000",
    }
    await _ingest(db_session, [moved])

    rows = (
        (
            await db_session.execute(
                select(MarketEventValue).order_by(
                    MarketEventValue.metric_name, MarketEventValue.period
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(row.metric_name, row.period) for row in rows] == [
        ("eps", "Q1-2027"),
        ("revenue", "Q1-2027"),
    ]
    assert {row.actual for row in rows if row.metric_name == "eps"} == {Decimal("2.30")}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_period_revision_drops_omitted_metric_and_earnings_reader_serves_current_value(
    db_session, monkeypatch
):
    """A current Benzinga revision is the only value state readers can see."""
    from app.mcp_server.tooling.fundamentals import _financials

    await _ingest(db_session, [EARNINGS_ROW])
    revised = {
        **EARNINGS_ROW,
        "updated": 1766188000,
        "period": "Q1",
        "period_year": 2027,
        "eps": "2.30",
        "eps_est": "2.25",
        "revenue": None,
        "revenue_est": None,
        "revenue_prior": None,
    }
    await _ingest(db_session, [revised])

    rows = (
        (
            await db_session.execute(
                select(MarketEventValue).order_by(MarketEventValue.metric_name)
            )
        )
        .scalars()
        .all()
    )
    assert [(row.metric_name, row.period) for row in rows] == [("eps", "Q1-2027")]
    assert rows[0].actual == Decimal("2.30")

    class _SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *_exc_info):
            return False

    monkeypatch.setattr(_financials, "AsyncSessionLocal", _SessionContext)
    result = await _financials._fetch_earnings_calendar_market_events_us(
        "AAPL", date(2026, 10, 1), date(2026, 10, 31)
    )

    assert result is not None
    assert result["count"] == 1
    earnings = result["earnings"][0]
    assert (earnings["quarter"], earnings["year"]) == (1, 2027)
    assert earnings["eps_actual"] == 2.30
    assert earnings["eps_estimate"] == 2.25
    assert earnings["revenue_actual"] is None
    assert earnings["revenue_estimate"] is None
