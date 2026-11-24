"""hk 6145 — session minute storage migration.

CI only for the DB part: it uses the pytest-owned throwaway database and runs
the whole round trip inside one transaction that is rolled back. The static
tests at the bottom need no database.

Covered: upgrade/downgrade round trip, ``KRX_AFTER`` accepted, an unknown
segment rejected, downgrade refused while rows exist, and (when timescaledb is
installed) the US continuous aggregates exist and a 5m bucket never crosses a
session boundary.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261124_hk6145_session_minutes.py"
KR_BASE_PATH = REPO / "alembic" / "versions" / "20260803_research_kr_candles.py"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- static ----


@pytest.mark.unit
def test_cagg_definition_keeps_segment_in_group_by_and_bucket_start() -> None:
    migration = _load(MIGRATION_PATH, "hk6145_static")
    names = [name for name, _ in migration.US_CAGG_SPECS]
    assert names == [
        "us_candles_5m",
        "us_candles_15m",
        "us_candles_30m",
        "us_candles_1h",
    ]
    for name, interval in migration.US_CAGG_SPECS:
        sql = " ".join(migration._cagg_sql(name, interval).split())
        assert "GROUP BY bucket, symbol, venue, session_segment" in sql
        assert (
            "time_bucket( INTERVAL '"
            + interval
            + "', time_utc, 'America/New_York' ) AS bucket"
            in sql
        )
        assert "FROM research.us_candles_1m" in sql


@pytest.mark.unit
def test_migration_adds_no_retention_or_refresh_policy_and_guards_timescale() -> None:
    source = MIGRATION_PATH.read_text()
    assert "add_retention_policy" not in source
    assert "add_continuous_aggregate_policy" not in source
    assert source.count("extname = 'timescaledb'") >= 2
    assert "rolname = 'at_app'" in source


@pytest.mark.unit
def test_kr_check_is_added_not_valid_and_cagg_grants_are_guarded() -> None:
    source = MIGRATION_PATH.read_text()
    assert "NOT VALID" in source
    assert "GRANT SELECT ON research.{view_name} TO at_app" in source
    grant_block = source[source.index("GRANT SELECT ON research.{view_name}") - 400 :]
    assert (
        "extname = 'timescaledb'" in grant_block.split("$$")[0]
        or "extname = 'timescaledb'" in grant_block[:400]
    )


@pytest.mark.unit
@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_every_kr_table_statement_tolerates_a_missing_table(step: str) -> None:
    """A database stamped past 20260803 (CI chain tests) has no research.kr_candles_1m."""
    migration = _load(MIGRATION_PATH, f"hk6145_kr_guard_{step}")
    captured: list[str] = []
    migration.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    getattr(migration, step)()
    touching = [sql for sql in captured if "research.kr_candles_1m" in sql]
    assert touching
    for sql in touching:
        assert "to_regclass('research.kr_candles_1m') IS NOT NULL" in sql, sql[:200]
        # plpgsql plans a whole IF condition at once: the existence check and the
        # query on the table must be in different (nested) statements.
        assert "IS NOT NULL AND" not in sql, sql[:200]
        assert "AND EXISTS (SELECT 1 FROM research.kr_candles_1m" not in sql


@pytest.mark.unit
def test_revision_chain_and_length() -> None:
    migration = _load(MIGRATION_PATH, "hk6145_chain")
    assert migration.down_revision == "20261122_hk6026_market_event"
    assert len(migration.revision) <= 32


# -------------------------------------------------------------------- DB ----


def _run_step(sync_conn, step: str) -> None:
    migration = _load(MIGRATION_PATH, f"hk6145_db_{step}")
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, step)()


def _old_kr_table_ddl() -> str:
    """The real CREATE TABLE research.kr_candles_1m from 20260803 (old CHECK)."""
    migration = _load(KR_BASE_PATH, "hk6145_kr_base")
    captured: list[str] = []
    migration.op = SimpleNamespace(
        execute=lambda sql: captured.append(str(sql)),
        create_table=lambda *a, **k: None,
        create_index=lambda *a, **k: None,
        drop_index=lambda *a, **k: None,
        drop_table=lambda *a, **k: None,
    )
    migration.upgrade()
    (ddl,) = [s for s in captured if "CREATE TABLE research.kr_candles_1m (" in s]
    return ddl


KR_INSERT = (
    "INSERT INTO research.kr_candles_1m (time_utc, session_date_kst, symbol, venue, "
    "session_segment, source, open, high, low, close, volume, value, retrieved_at, "
    "batch_id) VALUES (:t, '2026-11-24', '005930', 'KRX', :seg, 'KIS', 1,1,1,1,1,1, "
    "now(), 'b')"
)
KR_INSERT_NULL_VALUE = KR_INSERT.replace("1,1,1,1,1,1,", "1,1,1,1,1,NULL,")
US_INSERT = (
    "INSERT INTO research.us_candles_1m (time_utc, session_date, symbol, venue, "
    "session_segment, source, open, high, low, close, volume, value, retrieved_at, "
    "batch_id) VALUES (:t, '2026-11-24', 'NVDA', :venue, :seg, 'KIS', 1,1,1,1,1,1, "
    "now(), 'b')"
)
T0 = datetime(2026, 11, 24, 14, 30, tzinfo=UTC)


async def _rejected(conn, sql: str, params: dict) -> bool:
    try:
        async with conn.begin_nested():
            await conn.execute(text(sql), params)
    except (IntegrityError, DBAPIError):
        return True
    return False


async def _downgrade_refusal(conn) -> str | None:
    """Error text if the downgrade is refused (savepoint rolled back), else None."""
    try:
        async with conn.begin_nested():
            await conn.run_sync(_run_step, "downgrade")
    except DBAPIError as exc:
        return str(exc)
    return None


@pytest_asyncio.fixture
async def conn():
    from app.core.db import engine
    from tests._run_owned_database import validate_run_owned_database_url

    validate_run_owned_database_url(engine.url)
    async with engine.connect() as connection:
        try:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS research"))
            await connection.execute(
                text("DROP TABLE IF EXISTS research.us_candles_1m CASCADE")
            )
            await connection.execute(
                text("DROP TABLE IF EXISTS research.kr_candles_1m CASCADE")
            )
            await connection.execute(text(_old_kr_table_ddl()))
            yield connection
        finally:
            await connection.rollback()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_round_trip_krx_after_unknown_segment_and_downgrade_refusal(conn) -> None:
    # Old schema: KRX_AFTER is not allowed yet.
    assert await _rejected(conn, KR_INSERT, {"t": T0, "seg": "KRX_AFTER"})

    await conn.run_sync(_run_step, "upgrade")
    # Added NOT VALID: no scan under ACCESS EXCLUSIVE; new rows are still checked.
    validated = (
        await conn.execute(
            text(
                "SELECT convalidated FROM pg_constraint WHERE "
                "conrelid = 'research.kr_candles_1m'::regclass AND contype = 'c' "
                "AND conname = 'ck_research_kr_candles_1m_session_segment'"
            )
        )
    ).scalar_one()
    assert validated is False

    await conn.execute(text(KR_INSERT), {"t": T0, "seg": "KRX_AFTER"})
    assert await _rejected(
        conn, KR_INSERT, {"t": T0.replace(minute=31), "seg": "BOGUS"}
    )

    for segment in ("US_PRE", "US_REGULAR", "US_POST", "US_DAY", "UNKNOWN"):
        await conn.execute(
            text(US_INSERT),
            {
                "t": T0.replace(
                    minute={
                        "US_PRE": 1,
                        "US_REGULAR": 2,
                        "US_POST": 3,
                        "US_DAY": 4,
                        "UNKNOWN": 5,
                    }[segment]
                ),
                "venue": "NASD",
                "seg": segment,
            },
        )
    assert await _rejected(
        conn, US_INSERT, {"t": T0.replace(minute=40), "venue": "NASD", "seg": "BOGUS"}
    )
    assert await _rejected(
        conn, US_INSERT, {"t": T0.replace(minute=41), "venue": "KRX", "seg": "US_PRE"}
    )
    # primary key (time_utc, symbol, venue)
    assert await _rejected(
        conn, US_INSERT, {"t": T0.replace(minute=1), "venue": "NASD", "seg": "US_PRE"}
    )

    # Downgrade refused while rows exist (us rows first, then only the KRX_AFTER row).
    refusal = await _downgrade_refusal(conn)
    assert refusal is not None and "rows exist" in refusal
    await conn.execute(text("DELETE FROM research.us_candles_1m"))
    refusal = await _downgrade_refusal(conn)
    assert refusal is not None and "KRX_AFTER" in refusal
    # value is nullable after the upgrade; a NULL-value row blocks restoring NOT NULL
    await conn.execute(text("DELETE FROM research.kr_candles_1m"))
    await conn.execute(text(KR_INSERT_NULL_VALUE), {"t": T0, "seg": "UNKNOWN"})
    refusal = await _downgrade_refusal(conn)
    assert refusal is not None and "NULL value" in refusal

    await conn.execute(text("DELETE FROM research.kr_candles_1m"))
    await conn.run_sync(_run_step, "downgrade")
    assert (
        await conn.execute(text("SELECT to_regclass('research.us_candles_1m')"))
    ).scalar_one() is None
    assert await _rejected(conn, KR_INSERT, {"t": T0, "seg": "KRX_AFTER"})
    # NOT NULL is back after the downgrade
    assert await _rejected(conn, KR_INSERT_NULL_VALUE, {"t": T0, "seg": "UNKNOWN"})

    # And back up again.
    await conn.run_sync(_run_step, "upgrade")
    await conn.execute(text(KR_INSERT), {"t": T0, "seg": "KRX_AFTER"})


@pytest.mark.asyncio
@pytest.mark.integration
async def test_us_caggs_exist_and_five_minute_bucket_never_crosses_a_session(
    conn,
) -> None:
    has_ts = (
        await conn.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")
        )
    ).first()
    if has_ts is None:
        pytest.skip("timescaledb is not installed in this database (CI postgres image)")

    await conn.run_sync(_run_step, "upgrade")
    views = set(
        (
            await conn.execute(
                text(
                    "SELECT view_name FROM timescaledb_information.continuous_aggregates "
                    "WHERE view_schema = 'research' AND view_name LIKE 'us_candles_%'"
                )
            )
        ).scalars()
    )
    assert views == {
        "us_candles_5m",
        "us_candles_15m",
        "us_candles_30m",
        "us_candles_1h",
    }

    # 09:25..09:34 ET (13:25..13:34 UTC in November? EST => 14:25..14:34 UTC).
    for minute in range(25, 35):
        segment = "US_PRE" if minute < 30 else "US_REGULAR"
        await conn.execute(
            text(US_INSERT),
            {
                "t": datetime(2026, 11, 24, 14, minute, tzinfo=UTC),
                "venue": "NASD",
                "seg": segment,
            },
        )
    rows = (
        await conn.execute(
            text(
                "SELECT bucket, session_segment, bar_count FROM research.us_candles_5m "
                "WHERE symbol = 'NVDA' ORDER BY bucket"
            )
        )
    ).all()
    assert [(r.session_segment, r.bar_count) for r in rows] == [
        ("US_PRE", 5),
        ("US_REGULAR", 5),
    ]
    assert len({r.bucket for r in rows}) == 2
