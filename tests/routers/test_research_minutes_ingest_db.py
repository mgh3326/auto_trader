"""hk 6343: ingest replay against a throwaway DB (CI only).

Acceptance 2: replaying the same batch inserts 0 rows and leaves ``retrieved_at``
(first receipt) unchanged. The route handler runs against the real writer; the
transaction is rolled back by the shared migration fixture.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.research_minutes_ingest import ingest_research_minutes
from app.schemas.research_minutes_ingest import (
    ResearchMinuteBarItem,
    ResearchMinutesIngestRequest,
)

# Old kr table + new objects created inside one rolled-back transaction.
from tests.test_hk6145_session_minutes_migration_db import _run_step, conn  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

BARS = [
    datetime(2026, 11, 24, 14, 30, tzinfo=UTC),
    datetime(2026, 11, 24, 14, 31, tzinfo=UTC),
]


def _request(received_at: datetime) -> ResearchMinutesIngestRequest:
    return ResearchMinutesIngestRequest(
        market="us",
        batch_id="go-kis-poll-1",
        bars=[
            ResearchMinuteBarItem(
                symbol="NVDA",
                venue="NASD",
                time_utc=start,
                open=Decimal("100"),
                high=Decimal("110"),
                low=Decimal("90"),
                close=Decimal("105"),
                volume=Decimal("10"),
                value=Decimal("1000"),
                received_at=received_at,
                source="KIS",
            )
            for start in BARS
        ],
    )


async def test_replay_through_the_route_inserts_zero_and_keeps_first_receipt(
    conn,  # noqa: F811
) -> None:
    await conn.run_sync(_run_step, "upgrade")
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        first_receipt = BARS[0] + timedelta(minutes=1)
        first = await ingest_research_minutes(_request(first_receipt), session)
        assert (first.inserted, first.already_present, first.rejected) == (2, 0, 0)
        assert [r.status for r in first.results] == ["inserted", "inserted"]

        replay = await ingest_research_minutes(
            _request(first_receipt + timedelta(minutes=4)), session
        )
        assert (replay.inserted, replay.already_present, replay.rejected) == (0, 2, 0)
        assert [r.status for r in replay.results] == [
            "already_present",
            "already_present",
        ]

        rows = (
            await conn.execute(
                text(
                    "SELECT time_utc, venue, session_segment, source, retrieved_at, "
                    "batch_id FROM research.us_candles_1m ORDER BY time_utc"
                )
            )
        ).all()
        assert [row.time_utc for row in rows] == BARS
        assert {row.venue for row in rows} == {"NASD"}
        assert {row.session_segment for row in rows} == {"US_REGULAR"}
        assert {row.source for row in rows} == {"KIS"}
        # First receipt survives the replay; the producer's later "now" is not stored.
        assert {row.retrieved_at for row in rows} == {first_receipt}
        assert {row.batch_id for row in rows} == {"go-kis-poll-1"}
    finally:
        await session.close()
