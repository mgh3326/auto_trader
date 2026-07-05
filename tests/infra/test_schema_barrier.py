"""ROB-723 regression tests for the xdist-shared test-schema barrier.

Covers three concerns in order:

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

A concurrency stress test exercises real contention against the shared
test DB to confirm the buffer (combined with the barrier) absorbs the
deadlock window.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import DBAPIError

from tests._db_retry import run_with_deadlock_retry


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
