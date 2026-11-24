"""hk 6148: futures collector writer against a throwaway DB (CI only).

Re-running the same bars inserts 0 rows and leaves ``retrieved_at`` (first receipt)
unchanged; the other research and public minute tables are not touched.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.research_candles import futures_minutes_collector as fc
from tests.test_hk6148_futures_minutes_migration_db import _run_step, conn  # noqa: F401

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T1 = datetime(2026, 11, 24, 3, 0, 0, tzinfo=UTC)
T2 = T1 + timedelta(minutes=5)


def _bars() -> list[fc.FuturesBar]:
    return [
        fc.FuturesBar(
            time_utc=datetime(2026, 11, 24, 0, minute, tzinfo=UTC),  # 09:0x KST
            symbol="MF200.2612",
            product="MF200",
            contract_month="2612",
            roll_state="front",
            session_segment="FUT_DAY",
            session_date=date(2026, 11, 24),
            source="KIS",
            open=Decimal("400.00"),
            high=Decimal("400.20"),
            low=Decimal("399.90"),
            close=Decimal("400.10"),
            volume=Decimal("5"),
        )
        for minute in (0, 1)
    ]


async def test_rerun_inserts_zero_and_keeps_first_retrieved_at(conn) -> None:  # noqa: F811
    await conn.run_sync(_run_step, "upgrade")
    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        others = {}
        for other in (
            "research.kr_candles_1m",
            "research.us_candles_1m",
            "public.kr_candles_1m",
        ):
            exists = (
                await conn.execute(text("SELECT to_regclass(:n)"), {"n": other})
            ).scalar_one()
            if exists is not None:
                others[other] = (
                    await conn.execute(text(f"SELECT count(*) FROM {other}"))
                ).scalar_one()

        writer = fc.SqlFuturesMinuteWriter(session)
        bars = _bars()
        assert await writer.insert(bars, batch_id="b1", retrieved_at=T1) == (2, 0)
        assert await writer.insert(bars, batch_id="b2", retrieved_at=T2) == (0, 2)

        rows = (
            await conn.execute(
                text(
                    "SELECT retrieved_at, batch_id, source, session_segment, "
                    "session_date, roll_state FROM research.kr_futures_candles_1m "
                    "ORDER BY time_utc"
                )
            )
        ).all()
        assert [(r.retrieved_at, r.batch_id) for r in rows] == [(T1, "b1")] * 2
        assert {(r.source, r.session_segment, r.roll_state) for r in rows} == {
            ("KIS", "FUT_DAY", "front")
        }
        assert {r.session_date for r in rows} == {date(2026, 11, 24)}
        for other, count in others.items():
            after = (
                await conn.execute(text(f"SELECT count(*) FROM {other}"))
            ).scalar_one()
            assert after == count
    finally:
        await session.close()
