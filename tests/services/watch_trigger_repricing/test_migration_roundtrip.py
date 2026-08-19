"""ROB-1290 r2 SHOULD 2 — the migration, run against a real database.

Two gaps this closes.

``the package's schema never went through alembic``
    ``_bootstrap_test_schema`` builds the test database from
    ``Base.metadata``, not by running the migration chain. So every other
    test in this package proves the *model* is right and proves nothing
    about the migration that has to produce it in production.
``the repo's existing roundtrip tests cannot run here``
    They shell out to a hard-coded ``REPO / ".venv/bin/alembic"``, which
    does not exist when the interpreter lives elsewhere -- and creating one
    in the worktree is exactly what the run conditions forbid. This drives
    ``alembic.operations`` in-process instead, so it runs wherever pytest
    runs.

What it actually does: takes the run-owned test database, runs the real
``downgrade()`` and ``upgrade()`` functions from the migration module
against a live connection, and checks the constraint's *behaviour* flips
both ways -- a row the widened CHECK accepts must be rejected once the
narrow one is back. Everything happens inside a transaction that is rolled
back, so the shared test schema is unchanged when the test finishes.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import pathlib
import uuid

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.models.base import Base

pytestmark = pytest.mark.integration

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20260820_rob1290_awaiting_reconcile_state.py"
)
TABLE = sa.text("review.watch_event_repricing_claims")
NOW = dt.datetime(2026, 8, 18, 0, 6, tzinfo=dt.UTC)

_INSERT = sa.text(
    """
    INSERT INTO review.watch_event_repricing_claims
        (event_uuid, symbol, market, generation, owner_token, claimed_by,
         state, claimed_at, lease_expires_at)
    VALUES
        (:event_uuid, :symbol, 'kr', 1, :owner_token, 'rob1290-roundtrip',
         :state, :now, :lease)
    """
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("rob1290_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_awaiting_reconcile(connection: sa.Connection) -> None:
    connection.execute(
        _INSERT,
        {
            "event_uuid": uuid.uuid4(),
            "symbol": f"RT{uuid.uuid4().hex[:4]}",
            "owner_token": uuid.uuid4(),
            "state": "awaiting_reconcile",
            "now": NOW,
            "lease": NOW + dt.timedelta(minutes=30),
        },
    )


def _accepts_awaiting_reconcile(connection: sa.Connection) -> bool:
    """Try the write in a savepoint so a rejection does not poison the tx."""
    savepoint = connection.begin_nested()
    try:
        _insert_awaiting_reconcile(connection)
    except IntegrityError:
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


def _roundtrip(connection: sa.Connection) -> list[str]:
    migration = _load_migration()
    context = MigrationContext.configure(
        connection=connection, opts={"target_metadata": Base.metadata}
    )
    trace: list[str] = []
    with Operations.context(context):
        trace.append(f"initial_accepts={_accepts_awaiting_reconcile(connection)}")
        migration.downgrade()
        trace.append(
            f"after_downgrade_accepts={_accepts_awaiting_reconcile(connection)}"
        )
        migration.upgrade()
        trace.append(f"after_upgrade_accepts={_accepts_awaiting_reconcile(connection)}")
    return trace


@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_against_the_real_database(
    _bootstrap_test_schema,
) -> None:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        trace = await connection.run_sync(_roundtrip)
        # Nothing this test did survives it.
        await session.rollback()

    assert trace == [
        # The shipped model already carries the widened CHECK ...
        "initial_accepts=True",
        # ... the real downgrade() narrows it, and the row is refused ...
        "after_downgrade_accepts=False",
        # ... and the real upgrade() widens it again.
        "after_upgrade_accepts=True",
    ]


@pytest.mark.asyncio
async def test_the_shared_schema_is_unchanged_afterwards(
    _bootstrap_test_schema,
) -> None:
    """The roundtrip rolls back, so the next test sees the widened CHECK."""
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        accepts = await connection.run_sync(_accepts_awaiting_reconcile)
        await session.rollback()
    assert accepts is True
