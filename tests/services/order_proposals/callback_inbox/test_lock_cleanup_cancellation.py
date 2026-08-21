"""W5 — a cancelled unlock still terminates the backend.

Adversarial review R27. ``release`` already refuses to pool a connection it
cannot prove it unlocked: it calls ``_discard``, which invalidates the
connection so PostgreSQL drops the backend and every advisory lock with it.

But ``_discard`` guarded each step with ``contextlib.suppress(Exception)``,
and ``asyncio.CancelledError`` is a ``BaseException``. A second cancellation
arriving while ``invalidate()`` is in flight therefore escaped ``_discard``
with:

* ``invalidate()`` not finished,
* ``close()`` never called,
* ``self._connection`` already ``None`` -- so the lock object reported itself
  released while a backend that may still hold the lock was alive and
  eligible for reuse.

That is the one outcome the module exists to prevent. Cancellation must still
propagate -- callers rely on it -- but the cleanup has to *finish first*.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.services.order_proposals.callback_inbox import locks as locks_module

pytestmark = pytest.mark.unit


class _Recorder:
    """A connection that is cancelled once, mid-cleanup, deterministically."""

    def __init__(self, *, cancel_on: str = "invalidate") -> None:
        self.events: list[str] = []
        self._cancel_on = cancel_on
        self._fired = False

    async def execute(self, *_args, **_kwargs):
        self.events.append("execute")
        raise OSError("connection reset while unlocking")

    async def commit(self) -> None:  # pragma: no cover - unlock fails first
        self.events.append("commit")

    async def invalidate(self) -> None:
        self.events.append("invalidate_started")
        if self._cancel_on == "invalidate" and not self._fired:
            self._fired = True
            raise asyncio.CancelledError
        self.events.append("invalidate_completed")

    async def close(self) -> None:
        self.events.append("close")


async def _release_with(connection) -> BaseException | None:
    lock = locks_module.PostgresJobAdvisoryLock()
    lock._connection = connection  # noqa: SLF001 - constructing the failure state
    try:
        await lock.release(1234)
    except BaseException as exc:  # noqa: BLE001 - the point is what escapes
        return exc
    return None


@pytest.mark.asyncio
async def test_a_cancellation_during_cleanup_does_not_abandon_the_backend() -> None:
    """R27 — the counterexample, at the seam it actually arrives from.

    The unlock is cancelled (that cancellation is the one owed back to the
    caller), the cleanup then parks inside ``invalidate``, and the outer task
    is cancelled twice more while it waits. None of that may reach the
    cleanup, and none of it may let the backend survive.
    """
    events: list[str] = []
    at_gate = asyncio.Event()
    gate = asyncio.Event()

    class _Gated:
        async def execute(self, *_args, **_kwargs):
            events.append("execute")
            raise asyncio.CancelledError

        async def commit(self) -> None:  # pragma: no cover - unlock fails
            pass

        async def invalidate(self) -> None:
            events.append("invalidate_started")
            at_gate.set()
            await gate.wait()
            events.append("invalidate_completed")

        async def close(self) -> None:
            events.append("close")

    lock = locks_module.PostgresJobAdvisoryLock()
    lock._connection = _Gated()  # noqa: SLF001

    task = asyncio.create_task(lock.release(1234))
    await asyncio.wait_for(at_gate.wait(), timeout=5)

    # Before the gate: the cleanup is unfinished and the holder still owns
    # the connection, because dropping it here is what orphans the backend.
    assert task.done() is False
    assert lock.closed is False, "the holder let go before cleanup finished"

    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False, "a cancellation abandoned the cleanup"

    gate.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert "invalidate_completed" in events, events
    assert "close" in events, events
    assert lock.closed is True


@pytest.mark.asyncio
async def test_a_spurious_inner_cancellation_is_retried_not_propagated() -> None:
    """A ``CancelledError`` from the driver call is not our cancellation.

    Nothing cancels the cleanup task, so a ``CancelledError`` surfacing from
    inside ``invalidate()`` came from the driver. Abandoning the backend over
    it would be the original bug wearing a different hat, so the cleanup
    retries and the caller sees no cancellation at all.
    """
    connection = _Recorder()
    escaped = await _release_with(connection)

    assert escaped is None
    assert connection.events.count("invalidate_started") == 2, connection.events
    assert "invalidate_completed" in connection.events
    assert "close" in connection.events


@pytest.mark.asyncio
async def test_the_holder_keeps_authority_until_cleanup_finishes() -> None:
    """R27 — no orphan: the reference must not be dropped early.

    A lock object that reports ``closed`` while its backend is alive is worse
    than one that reports nothing, because the only remaining handle on that
    backend is gone.
    """
    seen: list[bool] = []

    class _Watching(_Recorder):
        async def invalidate(self) -> None:
            seen.append(lock.closed)
            await super().invalidate()

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Watching()
    lock._connection = connection  # noqa: SLF001
    await lock.release(1234)

    # Both attempts happen while the object still owns the connection.
    assert seen == [False, False], seen
    # Cleanup finished, so the handle may be dropped now -- and only now.
    assert lock.closed is True
    assert connection.events.count("invalidate_started") == 2
    assert "close" in connection.events


@pytest.mark.asyncio
async def test_a_real_task_cancelled_twice_still_terminates() -> None:
    """R27 — the same thing through genuine ``Task.cancel()`` calls."""
    reached_invalidate = asyncio.Event()
    finish = asyncio.Event()
    events: list[str] = []

    class _Slow:
        async def execute(self, *_args, **_kwargs):
            raise OSError("connection reset while unlocking")

        async def commit(self) -> None:  # pragma: no cover
            pass

        async def invalidate(self) -> None:
            events.append("invalidate_started")
            reached_invalidate.set()
            await finish.wait()
            events.append("invalidate_completed")

        async def close(self) -> None:
            events.append("close")

    lock = locks_module.PostgresJobAdvisoryLock()
    lock._connection = _Slow()  # noqa: SLF001

    task = asyncio.create_task(lock.release(1234))
    await asyncio.wait_for(reached_invalidate.wait(), timeout=5)
    task.cancel()  # the second cancellation, delivered inside invalidate()
    await asyncio.sleep(0)
    finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "invalidate_completed" in events, events
    assert "close" in events, events


@pytest.mark.asyncio
async def test_an_uncancelled_failure_path_is_unchanged() -> None:
    """Control: the ordinary discard still invalidates then closes, once."""
    connection = _Recorder(cancel_on="never")
    escaped = await _release_with(connection)

    assert escaped is None
    assert connection.events == [
        "execute",
        "invalidate_started",
        "invalidate_completed",
        "close",
    ]


@pytest.mark.asyncio
async def test_unprovable_termination_is_fatal_not_a_pool_return() -> None:
    """No weaker fallback, and no quiet one either.

    If ``invalidate`` fails and the driver offers no way to prove the backend
    is gone, the safe options are exhausted. Returning the connection to the
    pool would strand a possibly-held lock for the life of the process, and
    swallowing the failure would hide that. So the failure surfaces, and the
    holder keeps its reference rather than handing the connection on.
    """
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
    )

    class _Broken(_Recorder):
        async def invalidate(self) -> None:
            self.events.append("invalidate_started")
            raise OSError("cannot invalidate")

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Broken(cancel_on="never")
    lock._connection = connection  # noqa: SLF001
    with pytest.raises(LockTerminationUnproven):
        await lock.release(1234)

    assert lock.closed is False, "an unterminated backend was handed on"


@pytest.mark.asyncio
async def test_a_driver_terminate_is_proof_enough() -> None:
    """When ``invalidate`` fails, the driver's own terminate is the fallback."""
    terminated: list[str] = []

    class _Driver:
        @staticmethod
        def terminate() -> None:
            terminated.append("driver")

    class _Raw:
        driver_connection = _Driver()

        @staticmethod
        def detach() -> None:
            terminated.append("detach")

        @staticmethod
        def invalidate() -> None:
            terminated.append("raw_invalidate")

    class _BrokenWithDriver(_Recorder):
        async def get_raw_connection(self):
            return _Raw()

        async def invalidate(self) -> None:
            self.events.append("invalidate_started")
            raise OSError("cannot invalidate")

    connection = _BrokenWithDriver(cancel_on="never")
    escaped = await _release_with(connection)

    assert escaped is None
    assert "driver" in terminated, terminated
    assert "close" in connection.events, connection.events


# ---------------------------------------------------------------------------
# the same property, against a real PostgreSQL backend
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_double_cancelled_release_leaves_no_lock_behind(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R27 — two backends, one real advisory lock, cancellation mid-cleanup.

    If the backend is not actually terminated it keeps the session-level
    lock for the life of the process, and the contender below can never take
    it. That is the observable consequence, so that is what is asserted.
    """
    from app.core import db

    key = int(uuid.uuid4().int % 2_000_000_000) + 1

    lock = locks_module.PostgresJobAdvisoryLock()
    assert await lock.try_acquire(key) is True
    held_by = await lock.backend_pid()

    # A contender cannot take it while it is held -- anti-vacuity for the
    # assertion at the end.
    contender = await db.engine.connect()
    try:
        taken = bool(
            (
                await contender.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        assert taken is False, "the lock was not actually held"

        # Force the unlock to fail for real (server-side), then have the
        # cleanup be cancelled the first time it tries to invalidate.
        monkeypatch.setattr(locks_module, "_RELEASE", text("SELECT no_such_fn()"))

        real_invalidate = type(lock.connection_for_test()).invalidate
        at_gate = asyncio.Event()
        gate = asyncio.Event()

        async def _gated_invalidate(self):
            at_gate.set()
            await gate.wait()
            await real_invalidate(self)

        monkeypatch.setattr(
            type(lock.connection_for_test()),
            "invalidate",
            _gated_invalidate,
            raising=True,
        )

        task = asyncio.create_task(lock.release(key))
        await asyncio.wait_for(at_gate.wait(), timeout=20)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert task.done() is False, "a cancellation abandoned the cleanup"
        gate.set()

        with pytest.raises(asyncio.CancelledError):
            await task

        # The holder's backend must be gone, so the key is free again.
        retaken = bool(
            (
                await contender.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        assert retaken is True, (
            "a backend that may still hold the lock outlived the cancelled cleanup"
        )
        await contender.execute(
            text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": key}
        )
        alive = (
            await contender.execute(
                text("SELECT count(*) FROM pg_stat_activity WHERE pid = :pid"),
                {"pid": held_by},
            )
        ).scalar_one()
        assert alive == 0, "the lock-holding backend is still alive"

        # ... and PostgreSQL is holding no advisory lock on that key for it.
        stranded = (
            await contender.execute(
                text(
                    "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                    "AND pid = :pid"
                ),
                {"pid": held_by},
            )
        ).scalar_one()
        assert stranded == 0, "an advisory lock outlived the cancelled cleanup"
        await contender.commit()
    finally:
        await contender.close()


# ---------------------------------------------------------------------------
# the same hazard on the acquire side, and on the ordinary close
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_ambiguous_acquire_terminates_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The server may have granted the lock before the client failed.

    ``try_acquire`` cannot tell "never taken" from "taken, result lost", so
    its error path has exactly the same obligation as ``release``: terminate,
    never pool. And it must survive a cancellation arriving during that
    cleanup for the same reason.
    """
    connection = _Recorder()

    class _Engine:
        async def connect(self):
            return connection

    from app.core import db

    monkeypatch.setattr(db, "engine", _Engine(), raising=True)

    lock = locks_module.PostgresJobAdvisoryLock()
    with pytest.raises(BaseException) as caught:  # noqa: PT011 - either is fine
        await lock.try_acquire(4321)

    assert isinstance(caught.value, OSError | asyncio.CancelledError)
    assert "invalidate_completed" in connection.events, connection.events
    assert "close" in connection.events, connection.events
    assert lock.closed is True


@pytest.mark.asyncio
async def test_a_repeatedly_cancelled_close_still_finishes() -> None:
    """The success path has to be cancellation-proof too.

    An unlocked backend is safe to pool, but only once ``close`` has actually
    handed it back. A cancellation that abandons the close mid-flight leaks
    the connection instead.
    """
    events: list[str] = []
    reached = asyncio.Event()
    finish = asyncio.Event()

    class _SlowClose:
        async def execute(self, *_args, **_kwargs):
            class _Result:
                @staticmethod
                def scalar_one():
                    return True

            return _Result()

        async def commit(self) -> None:
            events.append("commit")

        async def invalidate(self) -> None:  # pragma: no cover - not this path
            events.append("invalidate")

        async def close(self) -> None:
            events.append("close_started")
            reached.set()
            await finish.wait()
            events.append("close_completed")

    lock = locks_module.PostgresJobAdvisoryLock()
    lock._connection = _SlowClose()  # noqa: SLF001

    task = asyncio.create_task(lock.release(4321))
    await asyncio.wait_for(reached.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    finish.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "close_completed" in events, events


def test_the_cleanup_never_rewrites_the_cancellation_bookkeeping() -> None:
    """``uncancel()`` would hide a cancellation from an enclosing TaskGroup.

    The contract is "finish the cleanup, then re-deliver the cancellation",
    not "pretend it did not happen".
    """
    import inspect

    source = inspect.getsource(locks_module)
    assert "uncancel(" not in source
    assert "suppress(BaseException)" not in source


# ---------------------------------------------------------------------------
# telemetry must never outrank the cleanup
# ---------------------------------------------------------------------------


class _RaisingLogger:
    """A logger whose handler blows up, as a full disk or a bad sink would."""

    def __init__(self) -> None:
        self.attempted: list[str] = []

    def _fail(self, event: str, *_args, **_kwargs) -> None:
        self.attempted.append(event)
        raise RuntimeError("logging handler failed")

    error = _fail
    critical = _fail
    warning = _fail
    info = _fail
    debug = _fail


@pytest.mark.parametrize("unlock", ["raises", "returns_false"])
@pytest.mark.asyncio
async def test_a_raising_logger_cannot_preempt_the_cleanup(
    unlock: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R27 — order of operations: terminate first, report afterwards.

    Both failure paths logged *before* discarding the connection, so a
    logging handler that raises escaped with ``invalidate`` and ``close``
    never called and the holder's reference already dropped -- the same
    orphaned backend as the cancellation bug, reached without any
    cancellation at all. A telemetry failure cannot outrank the cleanup.
    """

    class _Connection(_Recorder):
        async def execute(self, *_args, **_kwargs):
            self.events.append("execute")
            if unlock == "raises":
                raise OSError("connection reset while unlocking")

            class _Result:
                @staticmethod
                def scalar_one():
                    return False  # a lock this backend did not hold

            return _Result()

        async def invalidate(self) -> None:
            self.events.append("invalidate_started")
            self.events.append("invalidate_completed")

    spy = _RaisingLogger()
    monkeypatch.setattr(locks_module, "logger", spy, raising=True)

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Connection(cancel_on="never")
    lock._connection = connection  # noqa: SLF001

    await lock.release(1234)  # the logging failure must not surface

    assert "invalidate_completed" in connection.events, connection.events
    assert "close" in connection.events, connection.events
    assert lock.closed is True
    assert spy.attempted, "the failure was never reported at all"


@pytest.mark.asyncio
async def test_simulate_process_death_keeps_authority_until_it_finishes() -> None:
    """The test helper has the same obligation as the real path.

    It models a kill, so it must not do the one thing a kill never does:
    drop the last handle on a backend that is still alive.
    """
    seen: list[bool] = []

    class _Watching(_Recorder):
        async def invalidate(self) -> None:
            seen.append(lock.closed)
            self.events.append("invalidate_started")
            self.events.append("invalidate_completed")

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Watching(cancel_on="never")
    lock._connection = connection  # noqa: SLF001

    await lock.simulate_process_death()

    assert seen == [False], seen
    assert "invalidate_completed" in connection.events
    assert "close" in connection.events
    assert lock.closed is True


@pytest.mark.asyncio
async def test_simulate_process_death_refuses_to_drop_an_unterminated_backend() -> None:
    """The helper obeys the same contract, including its failure half.

    Awaiting the retained cleanup is not enough: its result has to be read.
    A helper that clears the reference regardless proves nothing about the
    backend and destroys the only handle on it -- which is exactly the state
    the rest of this module refuses to reach.
    """
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
    )

    class _Broken(_Recorder):
        async def invalidate(self) -> None:
            self.events.append("invalidate_started")
            raise OSError("cannot invalidate")

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Broken(cancel_on="never")
    lock._connection = connection  # noqa: SLF001

    with pytest.raises(LockTerminationUnproven):
        await lock.simulate_process_death()

    assert lock.closed is False, "an unterminated backend was handed on"


@pytest.mark.asyncio
async def test_simulate_process_death_re_delivers_a_cancellation() -> None:
    """... and does not swallow the cancellation it absorbed while cleaning up."""
    events: list[str] = []
    at_gate = asyncio.Event()
    gate = asyncio.Event()

    class _Gated:
        async def get_raw_connection(self):  # pragma: no cover - not needed
            raise OSError("no raw connection")

        async def invalidate(self) -> None:
            events.append("invalidate_started")
            at_gate.set()
            await gate.wait()
            events.append("invalidate_completed")

        async def close(self) -> None:
            events.append("close")

    lock = locks_module.PostgresJobAdvisoryLock()
    lock._connection = _Gated()  # noqa: SLF001

    task = asyncio.create_task(lock.simulate_process_death())
    await asyncio.wait_for(at_gate.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert task.done() is False, "a cancellation abandoned the cleanup"
    gate.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert "invalidate_completed" in events, events
    assert "close" in events, events
    assert lock.closed is True
