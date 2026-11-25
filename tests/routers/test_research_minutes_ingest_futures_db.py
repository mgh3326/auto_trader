"""hk 6424: futures ingest replay against a throwaway DB (CI only).

Acceptance 1: a valid futures bar is inserted; replaying the same batch inserts 0
rows and leaves ``retrieved_at`` (first receipt) unchanged. The route handler runs
against the real futures writer; the futures master listing is fixed (no network).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.routers.research_minutes_ingest import ingest_research_minutes
from app.schemas.research_minutes_ingest import (
    ResearchMinuteBarItem,
    ResearchMinutesIngestRequest,
)
from app.services.research_candles import futures_listing

# Futures table + new objects created inside one rolled-back transaction.
from tests.test_hk6148_futures_minutes_migration_db import _run_step, conn  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

# Past dates on purpose: the route reads the real clock, and a receipt claim after
# it would be refused as future. 2026-11-24 09:00-09:01 KST, a day session.
BARS = [
    datetime(2026, 11, 24, 0, 0, tzinfo=UTC),
    datetime(2026, 11, 24, 0, 1, tzinfo=UTC),
]
_LISTING: dict[str, tuple[str, ...]] = {"MF200": ("2026-11", "2026-12", "2027-03")}


class _Listing:
    async def listed_months_by_product(self) -> dict[str, tuple[str, ...]]:
        return _LISTING


def _request(received_at: datetime) -> ResearchMinutesIngestRequest:
    return ResearchMinutesIngestRequest(
        market="futures",
        batch_id="fillwire-futures-1",
        bars=[
            ResearchMinuteBarItem(
                symbol="MF200.2612",
                venue="KRX",
                time_utc=start,
                open=Decimal("400.00"),
                high=Decimal("400.20"),
                low=Decimal("399.90"),
                close=Decimal("400.10"),
                volume=Decimal("5"),
                received_at=received_at,
                source="KIS",
            )
            for start in BARS
        ],
    )


async def test_replay_through_the_route_inserts_zero_and_keeps_first_receipt(
    conn,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(futures_listing, "FUTURES_LISTING", _Listing())
    await conn.run_sync(_run_step, "upgrade")
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        # Closed-bar rule: the last bar (00:01) closes at 00:02:10.
        first_receipt = BARS[-1] + timedelta(minutes=2)
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
                    "SELECT time_utc, symbol, product, contract_month, roll_state, "
                    "session_segment, session_date, source, retrieved_at, batch_id "
                    "FROM research.kr_futures_candles_1m ORDER BY time_utc"
                )
            )
        ).all()
        assert [row.time_utc for row in rows] == BARS
        assert {row.symbol for row in rows} == {"MF200.2612"}
        assert {row.product for row in rows} == {"MF200"}
        assert {row.contract_month for row in rows} == {"2612"}
        # 2026-11 expired on 2026-11-12, so 2026-12 is the front contract here.
        assert {row.roll_state for row in rows} == {"front"}
        assert {row.session_segment for row in rows} == {"FUT_DAY"}
        assert {row.session_date for row in rows} == {date(2026, 11, 24)}
        assert {row.source for row in rows} == {"KIS"}
        # First receipt survives the replay; the producer's later claim is not stored.
        assert {row.retrieved_at for row in rows} == {first_receipt}
        assert {row.batch_id for row in rows} == {"fillwire-futures-1"}
    finally:
        await session.close()
