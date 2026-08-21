"""The processing authority for one durable callback job.

A PostgreSQL **session-level** advisory lock, held on a **dedicated**
``AsyncConnection`` for the entire handler execution. Three properties make
this the right primitive and each of them is load-bearing:

``session`` scope, not ``xact``
    The callback core commits several times while it runs (nonce consumption,
    the commit lease, rung transitions, the approval record). A
    ``pg_try_advisory_xact_lock`` would be released by the very first of those
    commits -- including the worker's own ``processing`` commit -- re-opening
    the window the lock exists to close.

a dedicated connection, not the ORM session
    A session-level lock taken on a pooled ORM connection outlives the
    ``AsyncSession``: the backend goes back to the pool still holding it, and
    the next unrelated checkout inherits an invisible lock. Owning the
    connection means the lock's lifetime is exactly the lock object's.

release-or-invalidate, never release-or-shrug
    If the unlock statement fails, or the task is cancelled mid-flight, the
    backend may still hold the lock. Returning it to the pool would strand
    that lock for the process's lifetime. So the failure path
    ``invalidate()``s the connection instead, which drops the socket; the
    backend exits and PostgreSQL releases every advisory lock it held.

Process death needs no cooperation at all: the socket closes, the backend
exits, the lock is gone, and the recovery sweep can reclaim the row.

Scope note: this fences *this application's* job processing. It is not broker
fencing, and it is not a distributed lock across a PostgreSQL restart -- see
``docs/runbooks/telegram-callback-durable-inbox.md`` for both limits.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Any, Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

logger = logging.getLogger(__name__)

_TRY_ACQUIRE = text("SELECT pg_try_advisory_lock(CAST(:key AS bigint))")
_RELEASE = text("SELECT pg_advisory_unlock(CAST(:key AS bigint))")
_BACKEND_PID = text("SELECT pg_backend_pid()")


class JobAdvisoryLock(Protocol):
    async def try_acquire(self, key: int) -> bool: ...

    async def release(self, key: int) -> None: ...


class PostgresJobAdvisoryLock:
    """Own one backend for as long as the job is being processed."""

    def __init__(self) -> None:
        self._connection: AsyncConnection | None = None

    @property
    def closed(self) -> bool:
        return self._connection is None

    def connection_for_test(self) -> AsyncConnection:
        """Expose the held connection so tests can assert on the real backend."""
        if self._connection is None:
            raise RuntimeError("advisory lock is not held")
        return self._connection

    async def backend_pid(self) -> int:
        """The PID of the backend actually holding the lock."""
        connection = self._connection
        if connection is None:
            raise RuntimeError("advisory lock is not held")
        return int((await connection.execute(_BACKEND_PID)).scalar_one())

    async def commit_for_test(self) -> None:
        """Commit on the lock's own connection.

        Exists so a test can prove the lock survives a commit -- the exact
        property a transaction-scoped lock would fail.
        """
        connection = self._connection
        if connection is None:
            raise RuntimeError("advisory lock is not held")
        await connection.commit()

    async def simulate_process_death(self) -> None:
        """Drop the socket without unlocking, exactly as a kill would."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        with contextlib.suppress(Exception):
            await connection.invalidate()
        with contextlib.suppress(Exception):
            await connection.close()

    async def try_acquire(self, key: int) -> bool:
        if self._connection is not None:
            raise RuntimeError("advisory lock instance is already acquired")
        from app.core import db

        connection = await db.engine.connect()
        try:
            acquired = bool(
                (await connection.execute(_TRY_ACQUIRE, {"key": key})).scalar_one()
            )
        except BaseException:
            # Never leave a connection behind on the error path; it may or may
            # not hold the lock, so drop the backend rather than pool it.
            await _discard(connection)
            raise
        if not acquired:
            # A refused acquirer holds nothing, so an ordinary close is right.
            with contextlib.suppress(Exception):
                await connection.close()
            return False
        self._connection = connection
        return True

    async def release(self, key: int) -> None:
        """Release and close, or discard the backend if that cannot be proved.

        Never raises: by the time this runs the job's outcome is already
        durable, and losing the connection must not turn a recorded outcome
        into an error.
        """
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            released = bool(
                (await connection.execute(_RELEASE, {"key": key})).scalar_one()
            )
            await connection.commit()
        except BaseException:
            logger.error(
                "order_proposals.telegram.callback_job_unlock_failed",
                extra={"lock_released": False},
            )
            await _discard(connection)
            return
        if not released:
            # We asked to release a lock this backend did not hold. Something
            # is wrong with our accounting, so do not hand the backend on.
            logger.error(
                "order_proposals.telegram.callback_job_unlock_not_held",
                extra={"lock_released": False},
            )
            await _discard(connection)
            return
        with contextlib.suppress(Exception):
            await connection.close()


async def _discard(connection: AsyncConnection) -> None:
    """Terminate a backend that might still hold a lock."""
    with contextlib.suppress(Exception):
        await connection.invalidate()
    with contextlib.suppress(Exception):
        await connection.close()


@contextlib.asynccontextmanager
async def job_advisory_lock(
    key: int, *, lock: JobAdvisoryLock | None = None
) -> AsyncIterator[bool]:
    """Hold the job lock for the whole body, releasing on every exit path.

    Cancellation included: ``finally`` runs on ``CancelledError`` too, and
    ``release`` discards the backend if it cannot prove the unlock, so a
    cancelled worker never strands a lock on a pooled connection.
    """
    holder: Any = lock if lock is not None else PostgresJobAdvisoryLock()
    acquired = await holder.try_acquire(key)
    if not acquired:
        yield False
        return
    try:
        yield True
    finally:
        await holder.release(key)


__all__ = [
    "JobAdvisoryLock",
    "PostgresJobAdvisoryLock",
    "job_advisory_lock",
]
