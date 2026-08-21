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

from .conftest import (
    held_lock_backend_pid_for_test,
    held_lock_connection_for_test,
    lock_is_released_for_test,
    quarantined_handles_for_test,
    simulate_lock_process_death_for_test,
)

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
    assert lock_is_released_for_test(lock) is False, (
        "the holder let go before cleanup finished"
    )

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
    assert lock_is_released_for_test(lock) is True


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
            seen.append(lock_is_released_for_test(lock))
            await super().invalidate()

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Watching()
    lock._connection = connection  # noqa: SLF001
    await lock.release(1234)

    # Both attempts happen while the object still owns the connection.
    assert seen == [False, False], seen
    # Cleanup finished, so the handle may be dropped now -- and only now.
    assert lock_is_released_for_test(lock) is True
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

    assert lock_is_released_for_test(lock) is False, (
        "an unterminated backend was handed on"
    )


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
    held_by = await held_lock_backend_pid_for_test(lock)

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

        real_invalidate = type(held_lock_connection_for_test(lock)).invalidate
        at_gate = asyncio.Event()
        gate = asyncio.Event()

        async def _gated_invalidate(self):
            at_gate.set()
            await gate.wait()
            await real_invalidate(self)

        monkeypatch.setattr(
            type(held_lock_connection_for_test(lock)),
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
    assert lock_is_released_for_test(lock) is True


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
    assert lock_is_released_for_test(lock) is True
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
            seen.append(lock_is_released_for_test(lock))
            self.events.append("invalidate_started")
            self.events.append("invalidate_completed")

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Watching(cancel_on="never")
    lock._connection = connection  # noqa: SLF001

    await simulate_lock_process_death_for_test(lock)

    assert seen == [False], seen
    assert "invalidate_completed" in connection.events
    assert "close" in connection.events
    assert lock_is_released_for_test(lock) is True


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
        await simulate_lock_process_death_for_test(lock)

    assert lock_is_released_for_test(lock) is False, (
        "an unterminated backend was handed on"
    )


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

    task = asyncio.create_task(simulate_lock_process_death_for_test(lock))
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
    assert lock_is_released_for_test(lock) is True


# ---------------------------------------------------------------------------
# R27d -- unproven termination must not close, and must not check in
# ---------------------------------------------------------------------------
#
# ``_terminate_now`` ran ``raw.invalidate()`` and the bookkeeping close
# unconditionally, including after both invalidate *and* the driver terminate
# had failed. So ``LockTerminationUnproven`` surfaced and the holder kept its
# reference -- but the wrapper underneath had already been closed and checked
# back into the pool, and the next checkout handed the same backend, still
# holding the advisory lock, to unrelated work. The retained reference was a
# phantom.


class _Driver:
    def __init__(self, *, terminates: bool, events: list[str]) -> None:
        self._terminates = terminates
        self._events = events

    def terminate(self) -> None:
        self._events.append("driver_terminate")
        if not self._terminates:
            raise OSError("driver terminate failed")


class _Raw:
    def __init__(self, *, terminates: bool, events: list[str]) -> None:
        self.driver_connection = _Driver(terminates=terminates, events=events)
        self._events = events

    def detach(self) -> None:
        self._events.append("detach")

    def invalidate(self) -> None:
        self._events.append("raw_invalidate")


class _Unterminable:
    """Neither ``invalidate`` nor the driver can prove the backend is gone."""

    def __init__(self, *, terminates: bool = False, unlock: str = "raises") -> None:
        self.events: list[str] = []
        self._terminates = terminates
        self._unlock = unlock
        self.raw = _Raw(terminates=terminates, events=self.events)

    async def get_raw_connection(self):
        self.events.append("get_raw_connection")
        return self.raw

    async def execute(self, *_args, **_kwargs):
        self.events.append("execute")
        if self._unlock == "raises":
            raise OSError("connection reset while unlocking")

        class _Result:
            @staticmethod
            def scalar_one():
                return True

        return _Result()

    async def commit(self) -> None:
        self.events.append("commit")

    async def invalidate(self) -> None:
        self.events.append("invalidate_started")
        raise OSError("cannot invalidate")

    async def close(self) -> None:
        self.events.append("close")


def _quarantined() -> set:
    return quarantined_handles_for_test()


@pytest.mark.asyncio
async def test_an_unproven_release_never_closes_or_checks_in() -> None:
    """R27d — no close, no raw invalidate, no checkin. The handle is kept."""
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
    )

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Unterminable()
    lock._connection = connection  # noqa: SLF001

    with pytest.raises(LockTerminationUnproven):
        await lock.release(1234)

    assert "close" not in connection.events, connection.events
    assert "raw_invalidate" not in connection.events, connection.events
    # Detaching is allowed and wanted: it is what stops a checkin.
    assert connection.events == [
        "execute",
        "get_raw_connection",
        "invalidate_started",
        "detach",
        "driver_terminate",
    ], connection.events

    assert lock_is_released_for_test(lock) is False
    quarantine = _quarantined()
    assert connection in quarantine, "the connection was left to the garbage collector"
    assert connection.raw in quarantine
    assert connection.raw.driver_connection in quarantine


@pytest.mark.asyncio
async def test_an_unproven_ambiguous_acquire_keeps_its_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R27d — the acquire path has no holder to keep the reference for it.

    ``try_acquire`` owns its connection in a local, so once the function
    unwinds nothing points at it and the garbage collector is free to close
    it -- back into the pool, lock and all. It has to be owned explicitly.
    """
    from app.core import db
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
    )

    connection = _Unterminable()

    class _Engine:
        async def connect(self):
            return connection

    monkeypatch.setattr(db, "engine", _Engine(), raising=True)

    lock = locks_module.PostgresJobAdvisoryLock()
    with pytest.raises(LockTerminationUnproven):
        await lock.try_acquire(4321)

    assert "close" not in connection.events, connection.events
    assert "raw_invalidate" not in connection.events, connection.events
    assert connection in _quarantined()


@pytest.mark.asyncio
async def test_an_unproven_simulated_death_keeps_its_connection() -> None:
    """R27d — and so does the helper that models a kill."""
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
    )

    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Unterminable()
    lock._connection = connection  # noqa: SLF001

    with pytest.raises(LockTerminationUnproven):
        await simulate_lock_process_death_for_test(lock)

    assert "close" not in connection.events, connection.events
    assert lock_is_released_for_test(lock) is False
    assert connection in _quarantined()


@pytest.mark.asyncio
async def test_the_driver_fallback_runs_in_order_when_it_does_prove_it() -> None:
    """R27d — proven termination: exact order, and only then a close."""
    lock = locks_module.PostgresJobAdvisoryLock()
    connection = _Unterminable(terminates=True)
    lock._connection = connection  # noqa: SLF001

    await lock.release(1234)

    assert connection.events == [
        "execute",
        "get_raw_connection",
        "invalidate_started",
        "detach",
        "driver_terminate",
        "raw_invalidate",
        "close",
    ], connection.events
    assert lock_is_released_for_test(lock) is True
    assert connection not in _quarantined()


# ---------------------------------------------------------------------------
# R27d -- a close that does not finish is not a close that finished
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("failure", ["runtime_error", "cancelled"])
@pytest.mark.asyncio
async def test_an_inner_close_failure_is_not_reported_as_success(
    failure: str,
) -> None:
    """R27d SHOULD — the unlock succeeded, so this is a checkout leak.

    ``_close_quietly`` swallowed everything, including a ``CancelledError``
    raised by the driver, and the retained wrapper reported success anyway.
    ``release`` then cleared the holder while the connection was neither
    closed nor checked in. The lock is already released, so no order can be
    submitted twice -- but "cleanup genuinely finished" has to mean it.
    """
    events: list[str] = []

    class _BadClose:
        async def get_raw_connection(self):
            raise OSError("no raw connection")

        async def execute(self, *_args, **_kwargs):
            class _Result:
                @staticmethod
                def scalar_one():
                    return True

            return _Result()

        async def commit(self) -> None:
            events.append("commit")

        async def invalidate(self) -> None:
            events.append("invalidate")

        async def close(self) -> None:
            events.append("close_attempted")
            if failure == "cancelled":
                raise asyncio.CancelledError
            raise RuntimeError("close failed")

    lock = locks_module.PostgresJobAdvisoryLock()
    lock._connection = _BadClose()  # noqa: SLF001

    await lock.release(1234)

    # The failed close must not simply be believed: the connection is
    # invalidated instead, so nothing hands it back to the pool.
    assert "close_attempted" in events, events
    assert "invalidate" in events, "a connection that would not close was pooled"
    assert lock_is_released_for_test(lock) is True


# ---------------------------------------------------------------------------
# R27d -- against a real pool and a real backend
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unproven_backend_is_never_handed_back_by_the_pool(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R27d — the property the fakes stand in for, on a real QueuePool.

    One slot. Take the lock, make termination unprovable, and the pool must
    not be able to hand that backend to anything else: the checkout blocks
    rather than returning the locked session, and an independent backend can
    still see the lock held. Only an explicit kill frees it.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    from app.core import db
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
    )

    key = int(uuid.uuid4().int % 2_000_000_000) + 1
    pooled = create_async_engine(
        settings.DATABASE_URL, pool_size=1, max_overflow=0, pool_timeout=2
    )
    observer = await db.engine.connect()
    try:
        monkeypatch.setattr(db, "engine", pooled, raising=True)
        lock = locks_module.PostgresJobAdvisoryLock()
        assert await lock.try_acquire(key) is True
        held_by = await held_lock_backend_pid_for_test(lock)

        connection_type = type(held_lock_connection_for_test(lock))
        monkeypatch.setattr(locks_module, "_RELEASE", text("SELECT no_such_fn()"))

        async def _no_invalidate(self):
            raise OSError("cannot invalidate")

        async def _no_raw(self):
            raise OSError("no raw connection")

        monkeypatch.setattr(connection_type, "invalidate", _no_invalidate)
        monkeypatch.setattr(connection_type, "get_raw_connection", _no_raw)

        with pytest.raises(LockTerminationUnproven):
            await lock.release(key)

        # The one slot is still occupied by a backend that may hold the lock.
        with pytest.raises(Exception) as checkout:
            spare = await pooled.connect()
            await spare.close()
        assert "timeout" in str(checkout.value).lower(), str(checkout.value)

        # An independent backend confirms the lock really is still held.
        still_held = bool(
            (
                await observer.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        assert still_held is False, "the lock was released by an unproven cleanup"

        # Explicit cleanup, which is what an operator would have to do.
        await observer.execute(
            text("SELECT pg_terminate_backend(:pid)"), {"pid": held_by}
        )
        # ``pg_terminate_backend`` signals the backend; PostgreSQL releases
        # its session locks once that backend exits. Bound the real server
        # handoff rather than treating one immediate observer query as proof
        # that the fatal R27 path leaked authority.
        retaken = False
        for _ in range(20):
            retaken = bool(
                (
                    await observer.execute(
                        text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"),
                        {"k": key},
                    )
                ).scalar_one()
            )
            if retaken:
                break
            await asyncio.sleep(0.05)
        assert retaken is True
        await observer.execute(
            text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": key}
        )
        await observer.commit()
    finally:
        await observer.close()
        await pooled.dispose()
