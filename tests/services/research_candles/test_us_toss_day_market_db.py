"""hk 6322: Toss US day-market minutes against a throwaway DB (CI only).

Covered: the additive venue migration (upgrade accepts ``TOSS_INTEGRATED``, rejects
an unknown venue, allows NULL ``value`` for Toss rows), replay of the same Toss bars
inserts 0 rows and keeps ``retrieved_at`` (first receipt), and the downgrade refuses
while ``TOSS_INTEGRATED`` rows exist. Runs inside one rolled-back transaction, like
``test_hk6145_session_minutes_migration_db``.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.toss.dto import TossMinuteCandle
from app.services.brokers.toss.market_calendar import parse_us_market_calendar
from app.services.research_candles import session_minutes_collector as sc

# 6145 fixture: old kr table + the 6145 upgrade on the research schema.
from tests.test_hk6145_session_minutes_migration_db import (  # noqa: F401
    _run_step,
    conn,
)

REPO = Path(__file__).resolve().parents[3]
MIG_A = REPO / "alembic/versions/20261125_hk6322_us_toss_venue.py"
MIG_B = REPO / "alembic/versions/20261125_hk6322_us_venue_val.py"

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T1 = datetime(2026, 11, 25, 6, 30, tzinfo=UTC)
T2 = datetime(2026, 11, 25, 6, 35, tzinfo=UTC)


def _apply(sync_conn, path: Path, step: str) -> None:
    spec = importlib.util.spec_from_file_location(f"hk6322_{step}", path)
    assert spec is not None and spec.loader is not None
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, step)()


async def _rejected(conn_, sql: str, params: dict) -> bool:
    try:
        async with conn_.begin_nested():
            await conn_.execute(text(sql), params)
    except (IntegrityError, DBAPIError):
        return True
    return False


US_INSERT = (
    "INSERT INTO research.us_candles_1m (time_utc, session_date, symbol, venue, "
    "session_segment, source, open, high, low, close, volume, value, retrieved_at, "
    "batch_id) VALUES (:t, '2026-11-25', 'NVDA', :venue, 'US_DAY', 'TOSS', 1,1,1,1,1, "
    "NULL, now(), 'b')"
)


async def _upgrade_all(conn_) -> None:
    await conn_.run_sync(_run_step, "upgrade")  # 6145: creates the us table
    await conn_.run_sync(_apply, MIG_A, "upgrade")
    await conn_.run_sync(_apply, MIG_B, "upgrade")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upgrade_accepts_toss_venue_and_rejects_unknown_venue(
    conn,  # noqa: F811
) -> None:
    await _upgrade_all(conn)
    assert not await _rejected(conn, US_INSERT, {"t": T1, "venue": "TOSS_INTEGRATED"})
    assert await _rejected(conn, US_INSERT, {"t": T1, "venue": "NOT_A_VENUE"})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_replay_of_the_same_toss_bars_inserts_zero_and_keeps_first_receipt(
    conn,  # noqa: F811
) -> None:
    await _upgrade_all(conn)
    calendar = parse_us_market_calendar(
        {
            "today": {
                "date": "2026-11-25",
                "dayMarket": {
                    "startTime": "2026-11-25T10:00:00+09:00",
                    "endTime": "2026-11-25T17:50:00+09:00",
                },
                "preMarket": None,
                "regularMarket": None,
                "afterMarket": None,
            }
        }
    )
    candle = TossMinuteCandle(
        timestamp=datetime.fromisoformat("2026-11-25T15:10:00+09:00"),
        open_price=Decimal("900.10"),
        high_price=Decimal("901.00"),
        low_price=Decimal("899.50"),
        close_price=Decimal("900.75"),
        volume=Decimal("100"),
        currency="USD",
    )
    bars = sc.bars_from_toss_candles(
        [candle],
        symbol="NVDA",
        observed_before=datetime.fromisoformat("2026-11-25T15:10:30+09:00"),
        us_calendar=calendar,
    ).bars
    assert len(bars) == 1

    session = AsyncSession(bind=conn, join_transaction_mode="create_savepoint")
    try:
        writer = sc.SqlMinuteWriter(session, "us")
        assert await writer.insert(bars, batch_id="first", retrieved_at=T1) == (1, 0)
        assert await writer.insert(bars, batch_id="replay", retrieved_at=T2) == (0, 1)
        rows = (
            await conn.execute(
                text(
                    "SELECT venue, source, session_segment, retrieved_at, value "
                    "FROM research.us_candles_1m WHERE symbol = 'NVDA'"
                )
            )
        ).all()
        assert len(rows) == 1
        venue, source, segment, retrieved_at, value = rows[0]
        assert (venue, source, segment, value) == (
            "TOSS_INTEGRATED",
            "TOSS",
            "US_DAY",
            None,
        )
        assert retrieved_at == T1  # first receipt, untouched by the replay
    finally:
        await session.close()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_downgrade_refused_while_toss_rows_exist(conn) -> None:  # noqa: F811
    await _upgrade_all(conn)
    await conn.execute(text(US_INSERT), {"t": T1, "venue": "TOSS_INTEGRATED"})
    with pytest.raises(DBAPIError) as refused:
        async with conn.begin_nested():
            await conn.run_sync(_apply, MIG_A, "downgrade")
    assert "TOSS_INTEGRATED" in str(refused.value)
