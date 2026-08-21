"""W5 — the job advisory lock, against real PostgreSQL backends.

Adversarial review R1, blocker 1. Every assertion here is a property of an
actual PostgreSQL server observed through two distinct backends: a boolean
fake or a transaction-scoped lock would pass a weaker test and ship the bug.

What is pinned:

* the lock is **session** scoped, on a **dedicated** connection, and survives
  a ``commit()`` — ``pg_try_advisory_xact_lock`` would be released by the
  processing-state commit, re-opening the door the lock exists to shut;
* a second backend cannot take the same key while it is held;
* an abrupt disconnect (the crash case) releases it, so recovery can reclaim;
* an unlock failure or a cancellation **invalidates** the connection instead
  of returning a lock-holding backend to the pool.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.integration


async def _acquire_probe(key: int) -> bool:
    """Ask a throwaway backend whether the key is currently free."""
    from app.core import db

    connection = await db.engine.connect()
    try:
        acquired = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
            )
        ).scalar_one()
        if acquired:
            await connection.execute(
                text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": key}
            )
        await connection.commit()
        return bool(acquired)
    finally:
        await connection.close()


async def _advisory_lock_pids(key: int) -> list[int]:
    """Real ``pg_locks`` rows for this exact bigint key, by backend PID."""
    from app.core import db

    classid = (key >> 32) & 0xFFFFFFFF
    objid = key & 0xFFFFFFFF
    connection = await db.engine.connect()
    try:
        rows = (
            await connection.execute(
                text(
                    "SELECT pid FROM pg_locks WHERE locktype = 'advisory' "
                    "AND classid = :classid AND objid = :objid AND objsubid = 1 "
                    "AND granted"
                ),
                {"classid": classid, "objid": objid},
            )
        ).all()
        await connection.commit()
        return sorted(row[0] for row in rows)
    finally:
        await connection.close()


def test_the_lock_key_is_a_stable_signed_bigint() -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )

    job_id = uuid.UUID("11111111-2222-4333-8444-555555555555")
    key = job_advisory_lock_key(job_id)
    assert key == job_advisory_lock_key(job_id), "key must be deterministic"
    assert -(2**63) <= key < 2**63
    assert key != job_advisory_lock_key(uuid.uuid4())


@pytest.mark.asyncio
async def test_the_lock_lives_on_its_own_backend_and_survives_a_commit(
    _bootstrap_test_schema,
) -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )

    key = job_advisory_lock_key(uuid.uuid4())
    lock = PostgresJobAdvisoryLock()
    assert await lock.try_acquire(key) is True
    try:
        holder_pids = await _advisory_lock_pids(key)
        assert len(holder_pids) == 1, holder_pids
        assert holder_pids == [await lock.backend_pid()]

        # The callback core commits several times while the lock is held. A
        # transaction-scoped lock would evaporate right here.
        await lock.commit_for_test()
        assert await _advisory_lock_pids(key) == holder_pids
        assert await _acquire_probe(key) is False
    finally:
        await lock.release(key)

    assert await _advisory_lock_pids(key) == []
    assert await _acquire_probe(key) is True


@pytest.mark.asyncio
async def test_a_second_holder_cannot_take_a_held_key(_bootstrap_test_schema) -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )

    key = job_advisory_lock_key(uuid.uuid4())
    first = PostgresJobAdvisoryLock()
    second = PostgresJobAdvisoryLock()
    assert await first.try_acquire(key) is True
    try:
        assert await second.try_acquire(key) is False
        # A refused acquirer must not hold a connection open either.
        assert second.closed is True
    finally:
        await first.release(key)
    assert await second.try_acquire(key) is True
    await second.release(key)


@pytest.mark.asyncio
async def test_an_abrupt_disconnect_releases_the_lock(_bootstrap_test_schema) -> None:
    """RED item 10, first half — a crashed worker's backend drops the lock."""
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )

    key = job_advisory_lock_key(uuid.uuid4())
    lock = PostgresJobAdvisoryLock()
    assert await lock.try_acquire(key) is True
    assert await _acquire_probe(key) is False

    await lock.simulate_process_death()

    assert await _advisory_lock_pids(key) == []
    assert await _acquire_probe(key) is True


@pytest.mark.asyncio
async def test_an_unlock_failure_invalidates_instead_of_pooling_the_backend(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend that still holds the lock must never go back to the pool."""
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
    )

    key = job_advisory_lock_key(uuid.uuid4())
    lock = PostgresJobAdvisoryLock()
    assert await lock.try_acquire(key) is True

    connection = lock.connection_for_test()
    original_execute = connection.execute

    async def _explode(statement, *args, **kwargs):
        if "pg_advisory_unlock" in str(statement):
            raise RuntimeError("unlock statement failed")
        return await original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(connection, "execute", _explode)

    # release() must swallow the failure (the job outcome is already durable)
    # but it must not leave a lock-holding backend usable.
    await lock.release(key)

    assert lock.closed is True
    assert connection.invalidated or connection.closed
    # End-to-end proof: the key is genuinely free again.
    assert await _advisory_lock_pids(key) == []
    assert await _acquire_probe(key) is True


@pytest.mark.asyncio
async def test_cancellation_while_held_still_frees_the_key(
    _bootstrap_test_schema,
) -> None:
    """A cancelled worker task must not strand the lock on a pooled backend."""
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.locks import (
        PostgresJobAdvisoryLock,
        job_advisory_lock,
    )

    key = job_advisory_lock_key(uuid.uuid4())
    entered = asyncio.Event()

    async def _hold() -> None:
        async with job_advisory_lock(key, lock=PostgresJobAdvisoryLock()) as acquired:
            assert acquired is True
            entered.set()
            await asyncio.sleep(30)

    task = asyncio.create_task(_hold())
    await asyncio.wait_for(entered.wait(), timeout=10)
    assert await _acquire_probe(key) is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await _advisory_lock_pids(key) == []
    assert await _acquire_probe(key) is True
