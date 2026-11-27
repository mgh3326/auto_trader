"""hk 7037: KIS_DAY venue against a throwaway DB (CI only).

Covered: the additive expand + validate migrations accept ``KIS_DAY`` and still
refuse an unknown venue and the raw ``BAY`` code at the table; a KIS_DAY bar
stores under its own venue (PK distinct from the listing exchanges); and the
downgrade refuses while KIS_DAY rows exist. Runs inside one rolled-back
transaction, like ``test_us_toss_day_market_db``.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

# 6145 fixture: old kr table + the 6145 upgrade on the research schema.
from tests.test_hk6145_session_minutes_migration_db import (  # noqa: F401
    _run_step,
    conn,
)

REPO = Path(__file__).resolve().parents[3]
MIG_6322 = REPO / "alembic/versions/20261125_hk6322_us_toss_venue.py"
MIG_6322_VAL = REPO / "alembic/versions/20261125_hk6322_us_venue_val.py"
MIG_EXPAND = REPO / "alembic/versions/20261127_hk7037_us_kis_day.py"
MIG_VALIDATE = REPO / "alembic/versions/20261127_hk7037_us_kis_day_val.py"

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

T1 = datetime(2026, 11, 25, 2, 0, tzinfo=UTC)

US_INSERT = (
    "INSERT INTO research.us_candles_1m (time_utc, session_date, symbol, venue, "
    "session_segment, source, open, high, low, close, volume, value, retrieved_at, "
    "batch_id) VALUES (:t, '2026-11-24', 'SPY', :venue, 'US_DAY', 'KIS', 1,1,1,1,1, "
    "1, now(), 'b')"
)


def _apply(sync_conn, path: Path, step: str) -> None:
    spec = importlib.util.spec_from_file_location(f"hk7037_{step}_{path.stem}", path)
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


async def _upgrade_all(conn_) -> None:
    await conn_.run_sync(_run_step, "upgrade")  # 6145: creates the us table
    await conn_.run_sync(_apply, MIG_6322, "upgrade")
    await conn_.run_sync(_apply, MIG_6322_VAL, "upgrade")
    await conn_.run_sync(_apply, MIG_EXPAND, "upgrade")
    await conn_.run_sync(_apply, MIG_VALIDATE, "upgrade")


async def test_upgrade_accepts_kis_day_and_still_refuses_raw_and_unknown_venues(
    conn,  # noqa: F811
) -> None:
    await _upgrade_all(conn)
    assert not await _rejected(conn, US_INSERT, {"t": T1, "venue": "KIS_DAY"})
    assert await _rejected(conn, US_INSERT, {"t": T1, "venue": "BAY"})
    assert await _rejected(conn, US_INSERT, {"t": T1, "venue": "NOT_A_VENUE"})


async def test_kis_day_bar_is_a_distinct_key_from_the_listing_exchanges(
    conn,  # noqa: F811
) -> None:
    await _upgrade_all(conn)
    await conn.execute(text(US_INSERT), {"t": T1, "venue": "KIS_DAY"})
    await conn.execute(text(US_INSERT), {"t": T1, "venue": "AMEX"})
    rows = (
        await conn.execute(
            text(
                "SELECT venue, session_segment FROM research.us_candles_1m "
                "WHERE symbol = 'SPY' ORDER BY venue"
            )
        )
    ).all()
    assert [tuple(r) for r in rows] == [("AMEX", "US_DAY"), ("KIS_DAY", "US_DAY")]


async def test_downgrade_refused_while_kis_day_rows_exist(conn) -> None:  # noqa: F811
    await _upgrade_all(conn)
    await conn.execute(text(US_INSERT), {"t": T1, "venue": "KIS_DAY"})
    with pytest.raises(DBAPIError) as refused:
        async with conn.begin_nested():
            await conn.run_sync(_apply, MIG_EXPAND, "downgrade")
    assert "KIS_DAY" in str(refused.value)
