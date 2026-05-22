"""ROB-284 — full migration round-trip must preserve row count.

This test is **server-only**: it shells out to ``alembic downgrade -3``
and ``alembic upgrade head``, which mutates the schema of the database
referenced by the project's alembic config. Running it against the
test DB collides with the create_all-driven fixture (see
``tests/conftest.py::db_session``) which is the canonical schema source
for the in-process test suite. Operators verify the round-trip manually
during ROB-284 deployment per the rollback runbook
``docs/runbooks/daily-candles-store.md``.

To run on a dedicated DB:

    DATABASE_URL=... uv run pytest \
      tests/services/daily_candles/test_migration_round_trip.py \
      -v -m slow --run-live  # plus a custom flag to opt-in
"""

from __future__ import annotations

import subprocess

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.skip(
    reason="blocker: server-only validation — alembic downgrade/upgrade "
    "shells against the project DB and collides with the test DB's "
    "create_all-driven schema. Operators verify round-trip on a real "
    "environment per docs/runbooks/daily-candles-store.md."
)
@pytest.mark.slow
@pytest.mark.asyncio
async def test_upgrade_downgrade_upgrade_preserves_rows(
    db_session: AsyncSession,
) -> None:
    before = (
        await db_session.execute(text("SELECT count(*) FROM crypto_candles_1d"))
    ).scalar_one()

    subprocess.run(["uv", "run", "alembic", "downgrade", "-3"], check=True)
    subprocess.run(["uv", "run", "alembic", "upgrade", "head"], check=True)

    after = (
        await db_session.execute(text("SELECT count(*) FROM crypto_candles_1d"))
    ).scalar_one()
    assert before == after
