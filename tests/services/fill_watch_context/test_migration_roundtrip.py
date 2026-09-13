"""Real run-owned database roundtrip for the additive #137 table."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.base import Base

pytestmark = pytest.mark.integration

MIGRATION_PATH = (
    pathlib.Path(__file__).resolve().parents[3]
    / "alembic"
    / "versions"
    / "20260908_task137_ctx_outcomes.py"
)
TABLE = "fill_watch_context_outcomes"
SCHEMA = "review"


def _load_migration():
    spec = importlib.util.spec_from_file_location(
        "task137_context_migration", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_table(connection: sa.Connection) -> bool:
    return sa.inspect(connection).has_table(TABLE, schema=SCHEMA)


def _roundtrip(connection: sa.Connection) -> list[str]:
    migration = _load_migration()
    context = MigrationContext.configure(
        connection=connection,
        opts={"target_metadata": Base.metadata},
    )
    trace = [f"initial={_has_table(connection)}"]
    with Operations.context(context):
        migration.downgrade()
        trace.append(f"after_downgrade={_has_table(connection)}")
        migration.upgrade()
        trace.append(f"after_upgrade={_has_table(connection)}")
    return trace


@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_uses_the_run_owned_pytest_database(
    _bootstrap_test_schema,
) -> None:
    """No URL is constructed here; pytest owns creation/drop of the database."""
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        trace = await connection.run_sync(_roundtrip)
        await session.rollback()

    assert trace == [
        "initial=True",
        "after_downgrade=False",
        "after_upgrade=True",
    ]


@pytest.mark.asyncio
async def test_roundtrip_rolls_back_to_the_bootstrapped_schema(
    _bootstrap_test_schema,
) -> None:
    from app.core.db import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        assert await connection.run_sync(_has_table) is True
        await session.rollback()
