"""W5 — the real Alembic chain, on a real, isolated PostgreSQL database.

Adversarial review R3, blocker 3. The sibling ``test_migration.py`` roundtrip
calls ``upgrade()``/``downgrade()`` as plain functions against a database whose
schema was built from ``Base.metadata``. That proves the DDL is reversible; it
proves nothing about the *chain* -- not that the revision applies from the
exact parent this branch is based on, not that ``alembic_version`` moves the
way it should, and not that a real ``alembic upgrade`` would succeed at all.

So this module drives the actual ``alembic`` CLI, in a subprocess, against a
scratch database created for this test and dropped afterwards:

    stamp 20260820_rob1290_reconcile
      -> upgrade head   (must land on the W5 revision, table + constraints live)
      -> downgrade 20260820_rob1290_reconcile   (table gone, version back)
      -> upgrade head   (idempotent; constraints live again)

Nothing here touches the run-owned test database, and nothing touches a
production database: the scratch name carries its own prefix and a random
suffix, and the fixture refuses to drop anything that does not match it.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REPO = pathlib.Path(__file__).resolve().parents[4]
PARENT_REVISION = "20260820_rob1290_reconcile"
W5_REVISION = "20260821_w5_callback_inbox"

_SCRATCH_PREFIX = "w5_alembic_chain_"


def _scratch_name() -> str:
    return f"{_SCRATCH_PREFIX}{uuid.uuid4().hex[:12]}"


def _admin_kwargs(url, *, database: str) -> dict[str, object]:
    return {
        "user": url.username,
        "password": url.password,
        "host": url.host,
        "port": url.port,
        "database": database,
        "timeout": 15,
    }


@pytest_asyncio.fixture
async def scratch_database() -> AsyncIterator[str]:
    """A private database, created and dropped by this test alone."""
    import asyncpg

    base = make_url(os.environ["DATABASE_URL"])
    name = _scratch_name()
    admin = await asyncpg.connect(**_admin_kwargs(base, database="postgres"))
    try:
        await admin.execute(f'CREATE DATABASE "{name}"')
    finally:
        await admin.close()

    target = await asyncpg.connect(**_admin_kwargs(base, database=name))
    try:
        # The migration creates its table in ``review``; the parent revision is
        # stamped rather than replayed, so nothing else has created it.
        await target.execute("CREATE SCHEMA IF NOT EXISTS review")
    finally:
        await target.close()

    url = base.set(database=name)
    try:
        yield url.render_as_string(hide_password=False)
    finally:
        assert name.startswith(_SCRATCH_PREFIX), name  # never drop anything else
        admin = await asyncpg.connect(**_admin_kwargs(base, database="postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def _alembic(*args: str, database_url: str) -> str:
    env = dict(os.environ)
    env["DATABASE_URL"] = database_url
    completed = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=str(_REPO),
        env=env,
        timeout=300,
    )
    assert completed.returncode == 0, (
        f"alembic {' '.join(args)} failed ({completed.returncode})\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return completed.stdout


async def _stamped_revision(database_url: str) -> str | None:
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        exists = await connection.fetchval(
            "SELECT to_regclass('public.alembic_version') IS NOT NULL"
        )
        if not exists:
            return None
        return await connection.fetchval("SELECT version_num FROM alembic_version")
    finally:
        await connection.close()


async def _table_exists(database_url: str) -> bool:
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        return bool(
            await connection.fetchval(
                "SELECT to_regclass('review.telegram_callback_inbox') IS NOT NULL"
            )
        )
    finally:
        await connection.close()


async def _live_objects(database_url: str) -> dict[str, list[str]]:
    """The constraints and indexes PostgreSQL actually has after the upgrade."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        constraints = await connection.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'review.telegram_callback_inbox'::regclass "
            "ORDER BY conname"
        )
        indexes = await connection.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'review' "
            "AND tablename = 'telegram_callback_inbox' ORDER BY indexname"
        )
        return {
            "constraints": [row[0] for row in constraints],
            "indexes": [row[0] for row in indexes],
        }
    finally:
        await connection.close()


async def _terminal_check_rejects_a_retained_nonce(database_url: str) -> bool:
    """Exercise the real constraint, not its name."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        try:
            await connection.execute(
                "INSERT INTO review.telegram_callback_inbox "
                "(job_id, update_digest, state, attempt_count, max_attempts, "
                " received_at, available_at, nonce) "
                "VALUES ($1, $2, 'succeeded', 0, 3, now(), now(), 'nonce123456')",
                uuid.uuid4(),
                uuid.uuid4().hex * 2,
            )
        except asyncpg.exceptions.CheckViolationError:
            return True
        return False
    finally:
        await connection.close()


def test_alembic_reports_exactly_one_head() -> None:
    """R3 B3 — asked of the CLI, not of a Python API that might differ."""
    output = (
        _alembic("heads", database_url=os.environ["DATABASE_URL"]).strip().splitlines()
    )
    heads = [line for line in output if line.strip()]
    assert len(heads) == 1, output
    assert heads[0].startswith(W5_REVISION), output


@pytest.mark.asyncio
async def test_the_real_chain_upgrades_downgrades_and_upgrades_again(
    scratch_database: str,
) -> None:
    expected_constraints = {
        "ck_telegram_callback_inbox_action",
        "ck_telegram_callback_inbox_active_reconstructable",
        "ck_telegram_callback_inbox_attempt_count",
        "ck_telegram_callback_inbox_error_class",
        "ck_telegram_callback_inbox_max_attempts",
        "ck_telegram_callback_inbox_outcome",
        "ck_telegram_callback_inbox_state",
        "ck_telegram_callback_inbox_terminal_scrubbed",
        "ck_telegram_callback_inbox_terminal_state_pending",
        "pk_telegram_callback_inbox",
        "uq_telegram_callback_inbox_job_id",
        "uq_telegram_callback_inbox_update_digest",
    }

    # -- start from the exact parent this branch is based on ----------------
    assert await _stamped_revision(scratch_database) is None
    _alembic("stamp", PARENT_REVISION, database_url=scratch_database)
    assert await _stamped_revision(scratch_database) == PARENT_REVISION
    assert await _table_exists(scratch_database) is False

    # -- upgrade ------------------------------------------------------------
    _alembic("upgrade", "head", database_url=scratch_database)
    assert await _stamped_revision(scratch_database) == W5_REVISION
    assert await _table_exists(scratch_database) is True

    objects = await _live_objects(scratch_database)
    assert expected_constraints <= set(objects["constraints"]), objects["constraints"]
    assert "ix_telegram_callback_inbox_state_available" in objects["indexes"], objects[
        "indexes"
    ]
    assert await _terminal_check_rejects_a_retained_nonce(scratch_database) is True

    # -- downgrade back to the exact parent ---------------------------------
    _alembic("downgrade", PARENT_REVISION, database_url=scratch_database)
    assert await _stamped_revision(scratch_database) == PARENT_REVISION
    assert await _table_exists(scratch_database) is False

    # -- and up again -------------------------------------------------------
    _alembic("upgrade", "head", database_url=scratch_database)
    assert await _stamped_revision(scratch_database) == W5_REVISION
    assert await _table_exists(scratch_database) is True

    objects = await _live_objects(scratch_database)
    assert expected_constraints <= set(objects["constraints"]), objects["constraints"]
    assert "ix_telegram_callback_inbox_state_available" in objects["indexes"]
    assert await _terminal_check_rejects_a_retained_nonce(scratch_database) is True
