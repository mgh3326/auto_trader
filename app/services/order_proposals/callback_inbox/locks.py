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

committed immediately, but still held
    The transaction opened by ``pg_try_advisory_lock`` is committed as soon
    as the result is read. A session-level advisory lock outlives the
    transaction that took it, so the lock is unaffected -- but the backend
    stops being ``idle in transaction``, which it otherwise would be for the
    entire handler. That matters because
    ``idle_in_transaction_session_timeout`` would then terminate it mid-job
    and release the lock without anything noticing.

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

    That cleanup has to *finish*, and cancellation is the reason it might
    not. ``asyncio.CancelledError`` is a ``BaseException``, so an ordinary
    ``suppress(Exception)`` does not hold it, and a second cancellation
    arriving mid-``invalidate`` used to escape with the backend still alive
    (R27). Every cleanup now runs in a retained task that this module never
    cancels, awaited through repeated ``asyncio.shield`` until it is done.
    The cancellation is remembered and re-delivered afterwards -- the
    contract is "finish, then re-deliver", never "pretend it did not
    happen", so ``uncancel`` is not called and the caller's cancellation
    semantics are unchanged.

    If neither ``invalidate()`` nor the driver's own ``terminate()`` can
    prove the backend is gone, there is no safe weaker option:
    :class:`LockTerminationUnproven` surfaces and the holder keeps its
    reference rather than pooling a connection that may still hold the lock.

Process death needs no cooperation at all: the socket closes, the backend
exits, the lock is gone, and the recovery sweep can reclaim the row.

Scope note: this fences *this application's* job processing. It is not broker
fencing, and it is not a distributed lock across a PostgreSQL restart -- see
``docs/runbooks/telegram-callback-durable-inbox.md`` for both limits.
"""

from __future__ import annotations

import asyncio
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


#: Handles for backends we could not prove are dead.
#:
#: A connection whose termination is unproven must never be closed, because
#: closing checks it back into the pool and the next caller inherits whatever
#: advisory lock it still holds. Not closing it is only half the job: nothing
#: else may drop the last reference either, or the garbage collector performs
#: the checkin on our behalf. ``try_acquire`` is the clearest case -- it owns
#: its connection in a local, so once the function unwinds the traceback is
#: the only thing holding it, and a traceback is a transient.
#:
#: So the handles are owned here, for the life of the process: the
#: ``AsyncConnection``, its raw handle, and the driver connection. The set only
#: grows on a path that also raises :class:`LockTerminationUnproven`, which is
#: fatal to the job, so it cannot grow quietly or without bound.
_QUARANTINE: set[Any] = set()


def quarantined_handles() -> set[Any]:
    """The handles held back from the pool. Exposed so tests can assert on it."""
    return _QUARANTINE


def _quarantine(*handles: Any) -> None:
    for handle in handles:
        if handle is not None:
            _QUARANTINE.add(handle)


class LockTerminationUnproven(RuntimeError):
    """A backend that may still hold the lock could not be killed.

    Deliberately fatal. The alternatives are pooling a connection that may
    hold an advisory lock -- stranding it for the life of the process -- or
    swallowing that fact, which hides it. By the time this can be raised the
    job's outcome is already durable, so the worker crashing is a recoverable
    event and the recovery sweep repairs the row.
    """


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
        if connection is None:
            return
        # Same obligation as the real path, results included: this models a
        # kill, so it must not do the one thing a kill never does -- drop the
        # last handle on a backend that is still alive.
        terminated, during = await _hard_discard(connection)
        if not terminated:
            raise LockTerminationUnproven("simulate_process_death")
        self._connection = None
        if during is not None:
            raise during

    async def try_acquire(self, key: int) -> bool:
        if self._connection is not None:
            raise RuntimeError("advisory lock instance is already acquired")
        from app.core import db

        connection = await db.engine.connect()
        try:
            acquired = bool(
                (await connection.execute(_TRY_ACQUIRE, {"key": key})).scalar_one()
            )
            if acquired:
                # Close the transaction, not the lock. A session-level
                # advisory lock survives ``COMMIT`` -- that is why this module
                # uses the session-scoped form -- so the lock is still held
                # afterwards and the backend stops being ``idle in
                # transaction`` for the whole handler (R30).
                #
                # If it stayed open and ``idle_in_transaction_session_timeout``
                # were shorter than a job, PostgreSQL would terminate the
                # backend, and that releases every advisory lock it held --
                # silently, while the coroutine that believes it owns the job
                # keeps running. Same-job exclusion would depend on a server
                # setting this repository does not control.
                #
                # Deliberately inside this ``try`` and before
                # ``self._connection`` is set: a failed commit is an ambiguous
                # acquire like any other, so it must terminate rather than
                # pool, and the handler must not run. Committing after the
                # boundary would open exactly the window this closes.
                await connection.commit()
        except BaseException as original:
            # The acquire is ambiguous, not failed: the server may have
            # granted the lock before the client lost the result. So this
            # path carries the same obligation as ``release`` -- terminate,
            # never pool -- and the same cancellation hazard, since the
            # cleanup can be interrupted just as easily here.
            terminated, during = await _hard_discard(connection)
            if not terminated:
                raise LockTerminationUnproven(str(key)) from original
            if during is not None and not isinstance(original, asyncio.CancelledError):
                raise during from original
            raise
        if not acquired:
            # A refused acquirer holds nothing, so an ordinary close is
            # right -- retained, so repeated cancellation cannot leak it.
            closed, during = await _retained_close(connection)
            if not closed:
                terminated, also = await _hard_discard(connection)
                during = during or also
                if not terminated:
                    raise LockTerminationUnproven(str(key))
            if during is not None:
                raise during
            return False
        self._connection = connection
        return True

    async def release(self, key: int) -> None:
        """Release and close, or terminate the backend if that cannot be proved.

        The reference is cleared only once the cleanup has actually finished.
        A lock object that reports itself released while its backend is alive
        is worse than one that reports nothing, because the last handle on
        that backend is then gone.

        Raises only what the caller must not miss: a re-delivered
        ``CancelledError``, or :class:`LockTerminationUnproven` when a
        possibly-locked backend could not be killed. An ordinary unlock
        failure is logged and handled, because by the time this runs the
        job's outcome is already durable and losing the connection must not
        turn a recorded outcome into an error.
        """
        connection = self._connection
        if connection is None:
            return

        original: BaseException | None = None
        released: bool | None = None
        report: str | None = None
        try:
            released = bool(
                (await connection.execute(_RELEASE, {"key": key})).scalar_one()
            )
            await connection.commit()
        except asyncio.CancelledError as cancelled:
            # Remembered, not swallowed: re-delivered once cleanup is done.
            original = cancelled
            report = "order_proposals.telegram.callback_job_unlock_cancelled"
        except BaseException:
            report = "order_proposals.telegram.callback_job_unlock_failed"

        if released is False:
            # We asked to release a lock this backend did not hold. Something
            # is wrong with our accounting, so do not hand the backend on.
            report = "order_proposals.telegram.callback_job_unlock_not_held"

        # Nothing is logged until the connection is dealt with. Reporting
        # first put a logging handler ahead of the cleanup, and a handler
        # that raises then orphaned a possibly-locked backend without any
        # cancellation being involved at all (R27).
        if released is True:
            # Unlocked for certain, so the backend is safe to hand back --
            # but only once the close has genuinely finished.
            closed, during = await _retained_close(connection)
            if not closed:
                # It is not holding the lock any more, so nothing can be
                # submitted twice; but a connection that would not close is
                # also not back in the pool, and pretending otherwise leaks
                # the checkout. Invalidate it instead, and only clear the
                # reference if that can be proven (R27d).
                terminated, also = await _hard_discard(connection)
                during = during or also
                if not terminated:
                    raise LockTerminationUnproven(str(key))
            self._connection = None
            escaping = original or during
            if escaping is not None:
                raise escaping
            return

        terminated, during = await _hard_discard(connection)
        if not terminated:
            # Keep the reference: pooling a connection that may still hold
            # the lock would strand it for the life of the process, and no
            # quieter option left is also safe.
            _report(
                logger.critical,
                "order_proposals.telegram.callback_job_backend_not_terminated",
            )
            raise LockTerminationUnproven(str(key))
        self._connection = None
        while_reporting = _report(logger.error, report)
        escaping = original or during or while_reporting
        if escaping is not None:
            raise escaping


def _report(level: Any, event: str | None) -> asyncio.CancelledError | None:
    """Emit one telemetry event, best effort.

    Called only after the connection has been dealt with. A logging handler
    that fails is a reporting problem; it must not become a lock problem, so
    the failure is dropped. A cancellation arriving here is handed back to
    the caller rather than lost, because by this point the cleanup is done
    and the cancellation is the only thing left owed.
    """
    if event is None:
        return None
    try:
        level(event, extra={"lock_released": False})
    except asyncio.CancelledError as cancelled:
        return cancelled
    except BaseException:
        return None
    return None


async def _await_retained(
    coroutine: Any,
) -> tuple[Any, asyncio.CancelledError | None]:
    """Run ``coroutine`` to completion no matter how often *we* are cancelled.

    The work goes into a task this module never cancels, and the shield is
    re-awaited until that task is done. Each cancellation delivered to us is
    remembered -- the first one is the one re-delivered -- but none of them
    reaches the cleanup, which is the whole point: a half-finished
    ``invalidate`` leaves a possibly-locked backend alive.

    ``uncancel`` is deliberately not called. Rewriting the cancelling count
    would hide the cancellation from an enclosing ``TaskGroup``; the contract
    here is to finish the cleanup and then re-deliver it unchanged.
    """
    task = asyncio.ensure_future(coroutine)
    cancelled: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            if cancelled is None:
                cancelled = exc
        except BaseException:
            # Raised by the task itself; it is done, so the loop ends here.
            break
    result = None
    if task.done() and not task.cancelled() and task.exception() is None:
        result = task.result()
    return result, cancelled


async def _terminate_now(connection: AsyncConnection) -> bool:
    """Kill a backend that might still hold a lock. True if that is proven.

    Never raises: it runs inside a retained task, so an exception here would
    only be observable as "the cleanup did not happen".
    """
    raw: Any = None
    driver: Any = None
    try:
        # Snapshotted first, through the public API, because a successful
        # invalidate detaches the connection and takes the handles with it.
        raw = await connection.get_raw_connection()
        driver = getattr(raw, "driver_connection", None)
    except BaseException:
        raw = None

    invalidated = False
    for _ in range(2):
        try:
            await connection.invalidate()
        except asyncio.CancelledError:
            # This task is never cancelled by us, so the cancellation came
            # from inside the driver call. Try again rather than abandon a
            # backend that may still hold the lock.
            continue
        except BaseException:
            break
        invalidated = True
        break

    if invalidated:
        # Proven: the backend is gone, so the bookkeeping close is safe.
        await _close_now(connection)
        return True

    # ``invalidate`` failed. An ordinary pool close is not an option -- it
    # would hand on a possibly-locked backend -- so try to prove termination
    # through the driver instead. Detach first, so nothing that follows can
    # check this connection in.
    if raw is not None:
        with contextlib.suppress(Exception):
            raw.detach()
    proven = False
    if driver is not None:
        try:
            driver.terminate()
        except BaseException:
            proven = False
        else:
            proven = True

    if not proven:
        # Both routes exhausted. Do nothing further: no raw invalidate, no
        # close, no checkin. The handles are kept alive explicitly so the
        # collector cannot finish the job for us, and the caller turns this
        # into a fatal error.
        _quarantine(connection, raw, driver)
        return False

    if raw is not None:
        with contextlib.suppress(Exception):
            raw.invalidate()
    await _close_now(connection)
    return True


async def _close_now(connection: AsyncConnection) -> bool:
    """Bookkeeping close. Returns whether it actually finished.

    Reporting the result matters even here: a close that raises leaves the
    connection neither closed nor checked in, and believing it succeeded
    leaks the checkout. The exception itself is dropped -- there is nothing
    useful to do with it at this depth -- but the failure is not.
    """
    try:
        await connection.close()
    except BaseException:
        return False
    return True


async def _hard_discard(
    connection: AsyncConnection,
) -> tuple[bool, asyncio.CancelledError | None]:
    """Terminate the backend, surviving any number of cancellations."""
    terminated, cancelled = await _await_retained(_terminate_now(connection))
    return bool(terminated), cancelled


async def _retained_close(
    connection: AsyncConnection,
) -> tuple[bool, asyncio.CancelledError | None]:
    """Close an unlocked connection, surviving any number of cancellations.

    Returns whether the close completed, so a caller cannot mistake "we tried"
    for "it is back in the pool".
    """
    completed, cancelled = await _await_retained(_close_now(connection))
    return bool(completed), cancelled


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
    "LockTerminationUnproven",
    "PostgresJobAdvisoryLock",
    "job_advisory_lock",
]
