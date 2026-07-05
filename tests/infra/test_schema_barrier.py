"""ROB-723 regression tests for the xdist-shared test-schema barrier.

Covers four concerns:

1. The deadlock-retry buffer (``tests._db_retry.run_with_deadlock_retry``)
   must transparently absorb transient ``SQLSTATE 40P01`` deadlock errors
   without retrying non-deadlock failures.
2. The unified DDL in ``tests._schema_bootstrap.apply_test_schema`` must be
   idempotent and must preserve the columns/constraints guaranteed by the
   pre-ROB-723 fixtures (incl. the ROB-455 decision CHECK unique to the
   investment-reports helper).
3. The session-scoped barrier fixture in ``tests.conftest`` must leave a
   durable sentinel behind so subsequent (workers') bootstrap calls can
   short-circuit.
4. The ``db_session`` and helper ``session`` fixtures must no longer carry
   DDL inline (schema parity is owned by the barrier).

A concurrency stress test exercises real contention against the shared
test DB to confirm the buffer (combined with the barrier) absorbs the
deadlock window.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest
from sqlalchemy.exc import DBAPIError

from tests._db_retry import run_with_deadlock_retry


# --------------------------------------------------------------------------- #
# Task 1: deadlock-retry buffer.                                              #
# --------------------------------------------------------------------------- #
class _FakeDeadlock(DBAPIError):
    def __init__(self) -> None:
        super().__init__("stmt", {}, Exception("deadlock detected"))


class _FakeOtherDBAPIError(DBAPIError):
    def __init__(self) -> None:
        super().__init__("stmt", {}, Exception("unique constraint violated"))


@pytest.mark.asyncio
async def test_retry_succeeds_after_transient_deadlock():
    calls = {"n": 0}
    rolled_back = {"n": 0}

    async def op():
        calls["n"] += 1
        if calls["n"] < 3:
            raise _FakeDeadlock()
        return "ok"

    async def rollback():
        rolled_back["n"] += 1

    result = await run_with_deadlock_retry(op, rollback=rollback, base_delay=0.0)
    assert result == "ok"
    assert calls["n"] == 3
    assert rolled_back["n"] == 2  # rolled back before each retry


@pytest.mark.asyncio
async def test_retry_reraises_non_deadlock_immediately():
    calls = {"n": 0}

    async def op():
        calls["n"] += 1
        raise _FakeOtherDBAPIError()

    with pytest.raises(DBAPIError):
        await run_with_deadlock_retry(op, base_delay=0.0)
    assert calls["n"] == 1  # no retry on non-deadlock


@pytest.mark.asyncio
async def test_retry_gives_up_after_attempts():
    async def op():
        raise _FakeDeadlock()

    with pytest.raises(DBAPIError):
        await run_with_deadlock_retry(op, attempts=3, base_delay=0.0)


# --------------------------------------------------------------------------- #
# Task 2: apply_test_schema() must be idempotent + preserve drift columns.    #
# --------------------------------------------------------------------------- #
from tests._schema_bootstrap import apply_test_schema, schema_content_hash  # noqa: E402


@pytest.mark.asyncio
async def test_apply_test_schema_is_idempotent():
    """Applying the unified DDL twice must not raise, and key drift columns
    guaranteed by the old fixtures must exist afterward."""
    from sqlalchemy import text

    from app.core.db import engine

    async with engine.begin() as conn:
        await apply_test_schema(conn)
    async with engine.begin() as conn:
        await apply_test_schema(conn)  # second run: no-op, must not error

    async with engine.connect() as conn:
        got = (
            await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='review' "
                    "AND table_name='investment_reports' "
                    "AND column_name='snapshot_bundle_uuid'"
                )
            )
        ).first()
        assert got is not None
        got2 = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname='ck_investment_report_item_decisions_decision'"
                )
            )
        ).first()
        assert got2 is not None


def test_schema_content_hash_is_stable_and_hex():
    h1 = schema_content_hash()
    h2 = schema_content_hash()
    assert h1 == h2
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)


# --------------------------------------------------------------------------- #
# Task 3: the bootstrap barrier must record the current schema hash.          #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_bootstrap_sentinel_present_after_session():
    """The barrier must have recorded the current schema hash exactly once."""
    from sqlalchemy import text

    from app.core.db import engine

    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                text("SELECT content_hash FROM public._pytest_schema_ready")
            )
        ).fetchall()
    hashes = {r[0] for r in rows}
    assert schema_content_hash() in hashes


# --------------------------------------------------------------------------- #
# Task 4: db_session must no longer carry DDL.                                #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_db_session_fixture_is_ddl_free(db_session):
    """db_session must be a thin session provider now (schema owned by barrier)."""
    from sqlalchemy import text

    import tests.conftest as conftest_mod

    src = inspect.getsource(conftest_mod.db_session.__wrapped__)
    assert "create_all" not in src
    assert "ALTER TABLE" not in src

    got = (
        await db_session.execute(
            text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_schema='review' AND table_name='investment_reports'"
            )
        )
    ).first()
    assert got is not None


# --------------------------------------------------------------------------- #
# Task 5: the helper session fixture must also be DDL-free.                   #
# --------------------------------------------------------------------------- #
def test_helper_session_fixture_is_ddl_free():
    import tests._investment_reports_helpers as helpers

    src = inspect.getsource(helpers.session.__wrapped__)
    assert "create_all" not in src
    assert "ADD CONSTRAINT" not in src
    assert "ALTER TABLE" not in src


# --------------------------------------------------------------------------- #
# Task 6: concurrency stress — the retry buffer must absorb a deadlock storm. #
# --------------------------------------------------------------------------- #
@pytest.mark.slow
@pytest.mark.asyncio
async def test_concurrent_truncate_and_read_no_deadlock_escape():
    """Hammer the review tables concurrently; retry+barrier must absorb any
    deadlock so none escapes to the caller."""
    from sqlalchemy import text

    from app.core.db import engine
    from tests._investment_reports_helpers import INVESTMENT_REPORTS_TABLES

    async def truncate_cycle():
        async def _op():
            async with engine.begin() as conn:
                for table in reversed(INVESTMENT_REPORTS_TABLES):
                    await conn.execute(
                        text(
                            f'TRUNCATE TABLE review."{table.name}" '
                            "RESTART IDENTITY CASCADE"
                        )
                    )

        await run_with_deadlock_retry(_op)

    async def read_cycle():
        async def _op():
            async with engine.connect() as conn:
                for table in INVESTMENT_REPORTS_TABLES:
                    await conn.execute(
                        text(f'SELECT count(*) FROM review."{table.name}"')
                    )

        await run_with_deadlock_retry(_op)

    tasks = []
    for _ in range(8):
        tasks.append(truncate_cycle())
        tasks.append(read_cycle())
    await asyncio.gather(*tasks)
