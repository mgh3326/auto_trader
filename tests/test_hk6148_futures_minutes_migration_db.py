"""hk 6148 — futures minute storage migration.

CI only for the DB part: it uses the pytest-owned throwaway database and runs the
whole round trip inside one transaction that is rolled back. The static tests need
no database.

Covered: upgrade/downgrade round trip, unknown segment/source/roll_state rejected,
primary key, NULL ``session_date`` allowed, downgrade refused while rows exist.
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
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261124_hk6148_futures_minutes.py"
TABLE = "research.kr_futures_candles_1m"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------- static ----


@pytest.mark.unit
def test_revision_chain_and_length() -> None:
    migration = _load(MIGRATION_PATH, "hk6148_chain")
    assert migration.down_revision == "20261124_hk6145_session_minutes"
    assert migration.revision == "20261124_hk6148_futures_min"
    assert len(migration.revision) <= 32


@pytest.mark.unit
def test_no_retention_no_refresh_guarded_hypertable_and_conditional_grant() -> None:
    source = MIGRATION_PATH.read_text()
    assert "add_retention_policy" not in source
    assert "add_continuous_aggregate_policy" not in source
    assert "extname = 'timescaledb'" in source
    assert "rolname = 'at_app'" in source
    assert "GRANT SELECT, INSERT ON research.kr_futures_candles_1m TO at_app" in source
    assert "DELETE" not in source.split("GRANT SELECT, INSERT")[1].split("$$")[0]


@pytest.mark.unit
def test_vocabularies_match_the_code() -> None:
    from app.services.kis_futures_quotes.contracts import ROLL_STATES
    from app.services.research_candles import session_segment as ss

    migration = _load(MIGRATION_PATH, "hk6148_vocab")
    assert migration.ROLL_STATES == ROLL_STATES
    assert migration.SEGMENTS == (ss.FUT_DAY, ss.FUT_NIGHT, ss.UNKNOWN)
    assert migration.SOURCES == ("KIS", "UNKNOWN")


@pytest.mark.unit
@pytest.mark.parametrize("step", ["upgrade", "downgrade"])
def test_migration_only_touches_the_new_table(step: str) -> None:
    migration = _load(MIGRATION_PATH, f"hk6148_scope_{step}")
    captured: list[str] = []
    migration.op = SimpleNamespace(execute=lambda sql: captured.append(str(sql)))
    getattr(migration, step)()
    joined = "\n".join(captured)
    assert "kr_futures_candles_1m" in joined
    for other in ("kr_candles_1m", "us_candles_1m", "public."):
        assert other not in joined.replace("kr_futures_candles_1m", "")


# -------------------------------------------------------------------- DB ----


def _run_step(sync_conn, step: str) -> None:
    migration = _load(MIGRATION_PATH, f"hk6148_db_{step}")
    context = MigrationContext.configure(sync_conn)
    with Operations.context(context):
        getattr(migration, step)()


INSERT = (
    f"INSERT INTO {TABLE} (time_utc, symbol, product, contract_month, roll_state, "
    "session_segment, session_date, source, open, high, low, close, volume, "
    "retrieved_at, batch_id) VALUES (:t, :symbol, 'MF200', :month, :roll, :seg, "
    ":session_date, :source, 1,1,1,1,1, now(), 'b')"
)
T0 = datetime(2026, 11, 24, 1, 0, tzinfo=UTC)


def _params(**overrides):
    base = {
        "t": T0,
        "symbol": "MF200.2612",
        "month": "2612",
        "roll": "front",
        "seg": "FUT_DAY",
        "session_date": "2026-11-24",
        "source": "KIS",
    }
    base.update(overrides)
    return base


async def _rejected(conn, params: dict) -> bool:
    try:
        async with conn.begin_nested():
            await conn.execute(text(INSERT), params)
    except (IntegrityError, DBAPIError):
        return True
    return False


@pytest_asyncio.fixture
async def conn():
    from app.core.db import engine
    from tests._run_owned_database import validate_run_owned_database_url

    validate_run_owned_database_url(engine.url)
    async with engine.connect() as connection:
        try:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS research"))
            await connection.execute(text(f"DROP TABLE IF EXISTS {TABLE} CASCADE"))
            yield connection
        finally:
            await connection.rollback()


@pytest.mark.asyncio
@pytest.mark.integration
async def test_round_trip_checks_and_downgrade_refusal(conn) -> None:
    await conn.run_sync(_run_step, "upgrade")

    for index, segment in enumerate(("FUT_DAY", "FUT_NIGHT", "UNKNOWN")):
        await conn.execute(
            text(INSERT),
            _params(
                t=T0.replace(minute=index),
                seg=segment,
                session_date=None if segment == "UNKNOWN" else "2026-11-25",
            ),
        )
    for state in ("front", "next", "expiring", "expired"):
        await conn.execute(
            text(INSERT), _params(t=T0.replace(hour=2), roll=state, symbol=f"X.{state}")
        )

    assert await _rejected(conn, _params(t=T0.replace(minute=10), seg="BOGUS"))
    assert await _rejected(conn, _params(t=T0.replace(minute=11), seg="KRX_REGULAR"))
    assert await _rejected(conn, _params(t=T0.replace(minute=12), source="TOSS"))
    assert await _rejected(conn, _params(t=T0.replace(minute=13), roll="rolling"))
    assert await _rejected(conn, _params(t=T0.replace(minute=14), month="26-12"))
    # primary key (time_utc, symbol)
    assert await _rejected(conn, _params(t=T0.replace(minute=0)))
    # same minute, different contract is a different row
    await conn.execute(
        text(INSERT), _params(t=T0.replace(minute=0), symbol="MF200.2703", month="2703")
    )

    with pytest.raises(DBAPIError, match="rows exist"):
        async with conn.begin_nested():
            await conn.run_sync(_run_step, "downgrade")

    await conn.execute(text(f"DELETE FROM {TABLE}"))
    await conn.run_sync(_run_step, "downgrade")
    assert (
        await conn.execute(text(f"SELECT to_regclass('{TABLE}')"))
    ).scalar_one() is None

    await conn.run_sync(_run_step, "upgrade")  # and back up again
    await conn.execute(text(INSERT), _params())
