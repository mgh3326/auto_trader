"""W5 — the real Alembic chain, on a real, isolated PostgreSQL database.

Adversarial review R3, blocker 3. The sibling ``test_migration.py`` roundtrip
calls ``upgrade()``/``downgrade()`` as plain functions against a database whose
schema was built from ``Base.metadata``. That proves the DDL is reversible; it
proves nothing about the *chain* -- not that the revision applies from the
exact parent this branch is based on, not that ``alembic_version`` moves the
way it should, and not that a real ``alembic upgrade`` would succeed at all.

So this module drives the actual ``alembic`` CLI, in a subprocess, against a
scratch database created for this test and dropped afterwards:

    build the parent schema -> stamp 20260820_rob1290_reconcile
      -> upgrade head   (W5 tables + later additive tables live; stamp = current head)
      -> downgrade 20260820_rob1290_reconcile   (W5 table gone, version back)
      -> upgrade head   (idempotent; constraints live again)

R9 B19 asked for the parent to be reached by replaying the real chain from an
empty database rather than stamped. **That is not achievable on this repo's
test infrastructure, and the deviation is deliberate.** Replaying from base
runs `alembic/versions/*_timescale*.py`, which abort with

    timescaledb extension version % is below required minimum 2.8.1

CI's database service is `postgres:15-alpine` (`.github/workflows/test.yml`),
which has no TimescaleDB at all, so a base replay can never be green there.
That constraint is why every pre-existing migration roundtrip in this
repository -- `paper_cohort`, `paper_evaluation` -- uses the same
create_all-plus-stamp construction.

What this file does instead closes B19's actual complaint, which was that the
parent schema was never *constructed*: the scratch database is materialised
from `Base.metadata` (the real schema, all schemas, every table), the objects
this branch adds after the parent are dropped so the migration genuinely
creates them, and only then is the parent stamped. The upgrade, the
downgrade and the re-upgrade are all real `alembic` CLI invocations against
that real schema.

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
from typing import TypedDict

import pytest
import pytest_asyncio
from sqlalchemy.engine import make_url

from app.models.rung_reason_vocabulary import RUNG_VOID_REASON_GROUPS
from tests._run_owned_database import validate_run_owned_database_url

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_REPO = pathlib.Path(__file__).resolve().parents[4]
PARENT_REVISION = "20260820_rob1290_reconcile"
HEAD_REVISION = "20260824_s257_rung_reason"

_SCRATCH_PREFIX = "w5_alembic_chain_"


class _CursorLiveObjects(TypedDict):
    constraints: list[str]
    indexes: list[str]
    columns: dict[str, tuple[str, str]]


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


#: Objects this branch adds *after* the parent revision. ``Base.metadata`` is
#: the current head, so they must be removed to reconstruct the parent-era
#: schema -- otherwise the migration collides with what create_all already
#: made. Same maintenance point the sibling roundtrip tests carry.
_POST_PARENT_TABLES: tuple[str, ...] = (
    "review.telegram_callback_recovery_cursor",
    "review.telegram_callback_inbox",
    "review.screener_pick_log",
)


@pytest_asyncio.fixture
async def scratch_database() -> AsyncIterator[str]:
    """A private database holding the real parent-era schema."""
    import asyncpg
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.models.base import Base

    base = validate_run_owned_database_url(os.environ["DATABASE_URL"])
    name = _scratch_name()
    try:
        admin = await asyncpg.connect(**_admin_kwargs(base, database="postgres"))
        try:
            await admin.execute(f'CREATE DATABASE "{name}"')
        finally:
            await admin.close()

        url = base.set(database=name)
        engine = create_async_engine(url.render_as_string(hide_password=False))
        try:
            async with engine.begin() as connection:
                for schema in ("paper", "research", "review"):
                    await connection.execute(
                        text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                    )
                await connection.run_sync(Base.metadata.create_all)
                for table in _POST_PARENT_TABLES:
                    await connection.execute(text(f"DROP TABLE IF EXISTS {table}"))
                # ROB-s257 E-2 is later than this reconstructed boundary.
                # Current metadata already contains its nullable observation
                # column, so drop it and let the migration add it back.
                await connection.execute(
                    text(
                        "ALTER TABLE review.order_proposal_rungs "
                        "DROP COLUMN void_reason_group"
                    )
                )
        finally:
            await engine.dispose()

        yield url.render_as_string(hide_password=False)
    finally:
        assert name.startswith(_SCRATCH_PREFIX), name  # never drop anything else
        admin = await asyncpg.connect(**_admin_kwargs(base, database="postgres"))
        try:
            await admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        finally:
            await admin.close()


def test_scratch_database_setup_cannot_bypass_its_bounded_cleanup() -> None:
    """A missing current-head table must fail assertions, not leak a scratch DB."""
    import ast
    import inspect
    import textwrap

    source = textwrap.dedent(inspect.getsource(scratch_database))
    tree = ast.parse(source)
    fixture = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef))
    lifecycle = next(node for node in fixture.body if isinstance(node, ast.Try))
    assert any(
        isinstance(node, ast.Yield)
        for statement in lifecycle.body
        for node in ast.walk(statement)
    )
    assert "DROP TABLE IF EXISTS {table}" in source
    assert "DROP DATABASE IF EXISTS" in "\n".join(
        ast.unparse(statement) for statement in lifecycle.finalbody
    )


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


async def _cursor_table_exists(database_url: str) -> bool:
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        return bool(
            await connection.fetchval(
                "SELECT to_regclass('review.telegram_callback_recovery_cursor') IS NOT NULL"
            )
        )
    finally:
        await connection.close()


async def _cursor_row_count(database_url: str) -> int:
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM review.telegram_callback_recovery_cursor"
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


async def _cursor_live_objects(database_url: str) -> _CursorLiveObjects:
    """The cursor's exact singleton constraints after a real upgrade."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        constraints = await connection.fetch(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'review.telegram_callback_recovery_cursor'::regclass "
            "ORDER BY conname"
        )
        indexes = await connection.fetch(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'review' "
            "AND tablename = 'telegram_callback_recovery_cursor' ORDER BY indexname"
        )
        columns = await connection.fetch(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'review' "
            "AND table_name = 'telegram_callback_recovery_cursor' "
            "ORDER BY ordinal_position"
        )
        return {
            "constraints": [row[0] for row in constraints],
            "indexes": [row[0] for row in indexes],
            "columns": {row[0]: (row[1], row[2]) for row in columns},
        }
    finally:
        await connection.close()


async def _assert_cursor_constraint_matrix(database_url: str) -> None:
    """Exercise singleton and ring bounds on the real migrated cursor table."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    cases = (
        ("lower_bound", 1, 0, True),
        ("upper_bound", 1, 3, True),
        ("wrong_singleton", 2, 0, False),
        ("negative_tier", 1, -1, False),
        ("tier_past_ring", 1, 4, False),
    )
    try:
        for name, row_id, next_tier, expected in cases:
            try:
                await connection.execute(
                    "INSERT INTO review.telegram_callback_recovery_cursor "
                    "(id, next_tier, updated_at) VALUES ($1, $2, now())",
                    row_id,
                    next_tier,
                )
            except asyncpg.exceptions.CheckViolationError:
                accepted = False
            else:
                accepted = True
                await connection.execute(
                    "DELETE FROM review.telegram_callback_recovery_cursor WHERE id = $1",
                    row_id,
                )
            if accepted != expected:
                raise AssertionError(f"cursor constraint matrix mismatch: {name}")
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


async def _other_parent_tables_exist(database_url: str) -> bool:
    """Anti-vacuity: the parent-era schema is genuinely present."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        for table in (
            "review.order_proposals",
            "review.order_proposal_rungs",
            "review.order_proposal_approval_batches",
        ):
            if not await connection.fetchval(
                f"SELECT to_regclass('{table}') IS NOT NULL"
            ):
                return False
        return True
    finally:
        await connection.close()


async def _assert_rung_reason_schema(database_url: str) -> None:
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        column = await connection.fetchrow(
            "SELECT data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'review' "
            "AND table_name = 'order_proposal_rungs' "
            "AND column_name = 'void_reason_group'"
        )
        assert column is not None
        assert column["data_type"] == "text"
        assert column["is_nullable"] == "YES"
        check_definitions = await connection.fetch(
            "SELECT pg_get_constraintdef(c.oid) AS definition "
            "FROM pg_constraint AS c "
            "WHERE c.conrelid = 'review.order_proposal_rungs'::regclass "
            "AND c.contype = 'c' "
            "AND pg_get_constraintdef(c.oid) "
            "ILIKE '%void_reason_group%'",
        )
        assert len(check_definitions) == 1
        check_definition = check_definitions[0]["definition"]
        assert isinstance(check_definition, str)
        assert all(
            f"'{group}'" in check_definition for group in RUNG_VOID_REASON_GROUPS
        )
    finally:
        await connection.close()


async def _processing_check_rejects_a_missing_started_at(database_url: str) -> bool:
    """R9 B19 + R7 B11 — the new invariant, exercised on the real schema."""
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
                " received_at, available_at, started_at, chat_id, telegram_user_id, action, "
                " subject_short, dispatch_attempt_id, membership_revision, "
                " membership_digest, nonce) "
                "VALUES ($1, $2, 'processing', 1, 3, now(), now(), NULL, '42', '777', "
                "'op', '0123abcd', $3, 1, 'abcdefghijkl', 'nonce123456')",
                uuid.uuid4(),
                uuid.uuid4().hex * 2,
                uuid.uuid4(),
            )
        except asyncpg.exceptions.CheckViolationError:
            return True
        return False
    finally:
        await connection.close()


async def _active_telegram_user_id_constraint_matrix(
    database_url: str,
) -> tuple[bool, str | None]:
    """Return the valid control and exact CHECK that rejects a missing user."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    try:
        valid_user_accepted = False
        try:
            await connection.execute(
                "INSERT INTO review.telegram_callback_inbox "
                "(job_id, update_digest, state, attempt_count, max_attempts, "
                " received_at, available_at, chat_id, telegram_user_id, action, "
                " subject_short, dispatch_attempt_id, membership_revision, "
                " membership_digest, nonce) "
                "VALUES ($1, $2, 'pending', 0, 3, now(), now(), '42', '777', "
                "'op', '0123abcd', $3, 1, 'abcdefghijkl', 'nonce123456')",
                uuid.uuid4(),
                uuid.uuid4().hex * 2,
                uuid.uuid4(),
            )
        except asyncpg.exceptions.CheckViolationError:
            valid_user_accepted = False
        else:
            valid_user_accepted = True

        try:
            await connection.execute(
                "INSERT INTO review.telegram_callback_inbox "
                "(job_id, update_digest, state, attempt_count, max_attempts, "
                " received_at, available_at, chat_id, telegram_user_id, action, "
                " subject_short, dispatch_attempt_id, membership_revision, "
                " membership_digest, nonce) "
                "VALUES ($1, $2, 'pending', 0, 3, now(), now(), '42', NULL, "
                "'op', '0123abcd', $3, 1, 'abcdefghijkl', 'nonce123456')",
                uuid.uuid4(),
                uuid.uuid4().hex * 2,
                uuid.uuid4(),
            )
        except asyncpg.exceptions.CheckViolationError as exc:
            return valid_user_accepted, exc.constraint_name
        return valid_user_accepted, None
    finally:
        await connection.close()


async def _attempt_budget_matrix(
    database_url: str,
) -> tuple[tuple[str, str, bool, bool], ...]:
    """Exercise R34's values on the real parent->head schema.

    Constraint-name parity is not enough here: the pre-R34 pair of names was
    present while a fixed-count row with a caller-supplied larger budget still
    passed.  This is intentionally the same all-state matrix as the ORM
    bootstrap test, but run after the real Alembic CLI reaches head.
    """
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    cases: tuple[tuple[str, object, object, bool], ...] = (
        ("fixed_lower_boundary", 0, 3, True),
        ("fixed_unspent_retry", 2, 3, True),
        ("fixed_upper_boundary", 3, 3, True),
        ("max_above_fixed", 3, 4, False),
        ("max_below_fixed", 0, 1, False),
        ("zero_max", 0, 0, False),
        ("negative_max", 0, -1, False),
        ("negative_attempt", -1, 3, False),
        ("attempt_above_fixed_budget", 4, 3, False),
        ("attempt_above_declared_budget", 2, 1, False),
        ("null_attempt", None, 3, False),
        ("null_max", 0, None, False),
    )
    states = (
        "pending",
        "processing",
        "retry_wait",
        "succeeded",
        "discarded",
        "dead_letter",
    )
    results: list[tuple[str, str, bool, bool]] = []
    try:
        for state in states:
            for name, candidate_count, maximum, expected in cases:
                active = state in {"pending", "processing", "retry_wait"}
                try:
                    await connection.execute(
                        "INSERT INTO review.telegram_callback_inbox "
                        "(job_id, update_digest, state, attempt_count, max_attempts, "
                        "received_at, available_at, started_at, chat_id, telegram_user_id, action, "
                        "subject_short, dispatch_attempt_id, membership_revision, "
                        "membership_digest, nonce, error_class) "
                        "VALUES ($1, $2, $3, $4, $5, now(), now(), "
                        "CASE WHEN $3 = 'processing' THEN now() ELSE NULL END, "
                        "$6, $7, $8, $9, $10, $11, $12, $13, $14)",
                        uuid.uuid4(),
                        uuid.uuid4().hex * 2,
                        state,
                        candidate_count,
                        maximum,
                        "42" if active else None,
                        "777" if active else None,
                        "op" if active else None,
                        "0123abcd" if active else None,
                        uuid.uuid4() if active else None,
                        1 if active else None,
                        "abcdefghijkl" if active else None,
                        "nonce123456" if active else None,
                        "pre_core_failure" if state == "retry_wait" else None,
                    )
                except (
                    asyncpg.exceptions.CheckViolationError,
                    asyncpg.exceptions.NotNullViolationError,
                ):
                    accepted = False
                else:
                    accepted = True
                expected_for_state = (
                    state != "retry_wait"
                    if name == "fixed_upper_boundary"
                    else expected
                )
                results.append((state, name, accepted, expected_for_state))
    finally:
        await connection.close()
    return tuple(results)


async def _assert_attempt_budget_matrix(database_url: str) -> None:
    for state, name, accepted, expected in await _attempt_budget_matrix(database_url):
        if accepted != expected:
            raise AssertionError(f"attempt budget matrix mismatch: {state}::{name}")


async def _error_class_constraint_matrix(
    database_url: str,
) -> tuple[tuple[str, bool, bool], ...]:
    """Exercise the closed error-class vocabulary on the real upgraded table."""
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    cases: tuple[tuple[str, str, bool], ...] = (
        ("attempt_budget_invalid", "attempt_budget_invalid", True),
        ("unknown", "r34_unknown_error_class", False),
    )
    results: list[tuple[str, bool, bool]] = []
    try:
        for name, error_class, expected in cases:
            try:
                await connection.execute(
                    "INSERT INTO review.telegram_callback_inbox "
                    "(job_id, update_digest, state, attempt_count, max_attempts, "
                    "received_at, available_at, error_class) "
                    "VALUES ($1, $2, 'dead_letter', 0, 3, now(), now(), $3::text)",
                    uuid.uuid4(),
                    uuid.uuid4().hex * 2,
                    error_class,
                )
            except asyncpg.exceptions.CheckViolationError:
                accepted = False
            else:
                accepted = True
            results.append((name, accepted, expected))
    finally:
        await connection.close()
    return tuple(results)


async def _assert_error_class_constraint_matrix(database_url: str) -> None:
    for name, accepted, expected in await _error_class_constraint_matrix(database_url):
        if accepted != expected:
            raise AssertionError(f"error-class vocabulary matrix mismatch: {name}")


async def _marker_check_rejects_a_verdict_without_entry(database_url: str) -> bool:
    """A recorded verdict with no handler entry must be unstorable."""
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
                " received_at, available_at, started_at, handler_completed_at, "
                " terminal_state_pending, chat_id, telegram_user_id, action, subject_short, "
                " dispatch_attempt_id, membership_revision, membership_digest, "
                " nonce) "
                "VALUES ($1, $2, 'processing', 1, 3, now(), now(), now(), now(), "
                "'succeeded', '42', '777', 'op', '0123abcd', $3, 1, 'abcdefghijkl', "
                "'nonce123456')",
                uuid.uuid4(),
                uuid.uuid4().hex * 2,
                uuid.uuid4(),
            )
        except asyncpg.exceptions.CheckViolationError:
            return True
        return False
    finally:
        await connection.close()


async def _outcome_constraint_matrix(
    database_url: str,
) -> tuple[tuple[str, bool, bool], ...]:
    """Run the R32 vocabulary matrix against the actual migrated table.

    Case names, rather than fixture values, are returned so a failure cannot
    echo the unknown lowercase slug or any payload fragment into test output.
    """
    import asyncpg

    url = make_url(database_url)
    connection = await asyncpg.connect(
        **_admin_kwargs(url, database=url.database or "")
    )
    cases: tuple[tuple[str, str | None, bool], ...] = (
        ("null", None, True),
        ("known", "approved", True),
        ("unclassified", "unclassified", True),
        ("unknown_valid_slug", "r32nonceopaquevalue", False),
        ("raw_known_prefix_payload", "approval_window:expired:opaque", False),
        ("unknown_prefix_payload", "unknown_outcome_family:opaque", False),
        ("uppercase", "APPROVED", False),
    )
    results: list[tuple[str, bool, bool]] = []
    try:
        for name, outcome, expected in cases:
            try:
                await connection.execute(
                    "INSERT INTO review.telegram_callback_inbox "
                    "(job_id, update_digest, state, attempt_count, max_attempts, "
                    "received_at, available_at, outcome) "
                    "VALUES ($1, $2, 'succeeded', 0, 3, now(), now(), $3::text)",
                    uuid.uuid4(),
                    uuid.uuid4().hex * 2,
                    outcome,
                )
            except asyncpg.exceptions.CheckViolationError:
                accepted = False
            else:
                accepted = True
            results.append((name, accepted, expected))
    finally:
        await connection.close()
    return tuple(results)


async def _assert_outcome_constraint_matrix(database_url: str) -> None:
    for name, accepted, expected in await _outcome_constraint_matrix(database_url):
        if accepted != expected:
            raise AssertionError(f"outcome vocabulary matrix mismatch: {name}")


def test_alembic_reports_exactly_one_head() -> None:
    """R3 B3 — asked of the CLI, not of a Python API that might differ."""
    output = (
        _alembic("heads", database_url=os.environ["DATABASE_URL"]).strip().splitlines()
    )
    heads = [line for line in output if line.strip()]
    assert len(heads) == 1, output
    assert heads[0].startswith(HEAD_REVISION), output


@pytest.mark.asyncio
async def test_the_real_chain_upgrades_downgrades_and_upgrades_again(
    scratch_database: str,
) -> None:
    """R9 B19 — real parent schema -> head -> parent -> head, all via the CLI."""
    # R23: the EXACT live set, not a subset. The previous `<=` check was
    # missing `handler_marker_order` entirely, so a constraint could vanish
    # from the migration without anything failing.
    expected_constraints = {
        "ck_telegram_callback_inbox_action",
        "ck_telegram_callback_inbox_active_reconstructable",
        "ck_telegram_callback_inbox_attempt_count",
        "ck_telegram_callback_inbox_error_class",
        "ck_telegram_callback_inbox_handler_marker_order",
        "ck_telegram_callback_inbox_max_attempts",
        "ck_telegram_callback_inbox_outcome",
        "ck_telegram_callback_inbox_processing_started_at",
        "ck_telegram_callback_inbox_retry_vocabulary",
        "ck_telegram_callback_inbox_retry_budget",
        "ck_telegram_callback_inbox_state",
        "ck_telegram_callback_inbox_terminal_scrubbed",
        "ck_telegram_callback_inbox_terminal_state_pending",
        "pk_telegram_callback_inbox",
        "uq_telegram_callback_inbox_job_id",
        "uq_telegram_callback_inbox_update_digest",
    }
    expected_cursor_constraints = {
        "ck_telegram_callback_recovery_cursor_id",
        "ck_telegram_callback_recovery_cursor_next_tier",
        "pk_telegram_callback_recovery_cursor",
    }

    # -- the parent-era schema really is there, minus what this branch adds --
    assert await _stamped_revision(scratch_database) is None
    assert await _other_parent_tables_exist(scratch_database) is True, (
        "the parent schema was not constructed; this test would prove nothing"
    )
    assert await _table_exists(scratch_database) is False
    assert await _cursor_table_exists(scratch_database) is False
    _alembic("stamp", PARENT_REVISION, database_url=scratch_database)
    assert await _stamped_revision(scratch_database) == PARENT_REVISION
    assert await _table_exists(scratch_database) is False, (
        "the inbox table exists at the parent revision; the chain is not additive"
    )
    assert await _cursor_table_exists(scratch_database) is False, (
        "the recovery cursor exists at the parent revision; the chain is not additive"
    )

    async def _check_head_state() -> None:
        await _assert_rung_reason_schema(scratch_database)
        assert await _table_exists(scratch_database) is True
        assert await _cursor_table_exists(scratch_database) is True
        assert await _cursor_row_count(scratch_database) == 0
        await _assert_cursor_constraint_matrix(scratch_database)
        assert await _cursor_row_count(scratch_database) == 0
        # This is deliberately performed before checking the revision label so
        # the inherited regex-only implementation fails on the actual database
        # behaviour, not merely because a revision label is absent.
        await _assert_outcome_constraint_matrix(scratch_database)
        await _assert_attempt_budget_matrix(scratch_database)
        await _assert_error_class_constraint_matrix(scratch_database)
        assert await _stamped_revision(scratch_database) == HEAD_REVISION
        objects = await _live_objects(scratch_database)
        live = {
            name
            for name in objects["constraints"]
            if name.startswith(("ck_telegram", "pk_telegram", "uq_telegram"))
        }
        assert live == expected_constraints, {
            "missing": sorted(expected_constraints - live),
            "unexpected": sorted(live - expected_constraints),
        }
        assert "ix_telegram_callback_inbox_state_available" in objects["indexes"], (
            objects["indexes"]
        )
        cursor_objects = await _cursor_live_objects(scratch_database)
        assert set(cursor_objects["constraints"]) == expected_cursor_constraints
        assert cursor_objects["indexes"] == ["pk_telegram_callback_recovery_cursor"]
        assert cursor_objects["columns"] == {
            "id": ("smallint", "NO"),
            "next_tier": ("smallint", "NO"),
            "updated_at": ("timestamp with time zone", "NO"),
        }
        assert await _terminal_check_rejects_a_retained_nonce(scratch_database) is True
        assert (
            await _processing_check_rejects_a_missing_started_at(scratch_database)
            is True
        )
        (
            valid_user_accepted,
            missing_user_constraint,
        ) = await _active_telegram_user_id_constraint_matrix(scratch_database)
        assert valid_user_accepted is True
        assert (
            missing_user_constraint
            == "ck_telegram_callback_inbox_active_reconstructable"
        )
        # R23: the marker-order constraint, exercised rather than merely named.
        assert (
            await _marker_check_rejects_a_verdict_without_entry(scratch_database)
            is True
        )

    # -- upgrade -------------------------------------------------------------
    _alembic("upgrade", "head", database_url=scratch_database)
    await _check_head_state()

    # -- downgrade back to the exact parent ----------------------------------
    _alembic("downgrade", PARENT_REVISION, database_url=scratch_database)
    assert await _stamped_revision(scratch_database) == PARENT_REVISION
    assert await _table_exists(scratch_database) is False
    assert await _cursor_table_exists(scratch_database) is False

    # -- and up again --------------------------------------------------------
    _alembic("upgrade", "head", database_url=scratch_database)
    await _check_head_state()
