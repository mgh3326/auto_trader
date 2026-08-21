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
    """R27 — the counterexample: cancelled mid-``invalidate``."""
    connection = _Recorder()
    escaped = await _release_with(connection)

    # Cancellation semantics are preserved: it still reaches the caller.
    assert isinstance(escaped, asyncio.CancelledError)

    # ... but not before the backend was actually terminated.
    assert "invalidate_completed" in connection.events, connection.events
    assert "close" in connection.events, connection.events


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
    with pytest.raises(asyncio.CancelledError):
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
async def test_cleanup_survives_an_invalidate_that_always_fails() -> None:
    """No weaker fallback: a failing invalidate must not become a pool return."""

    class _Broken(_Recorder):
        async def invalidate(self) -> None:
            self.events.append("invalidate_started")
            raise OSError("cannot invalidate")

    connection = _Broken(cancel_on="never")
    escaped = await _release_with(connection)

    assert escaped is None
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
        fired: list[int] = []

        async def _cancel_once(self):
            if not fired:
                fired.append(1)
                raise asyncio.CancelledError
            await real_invalidate(self)

        monkeypatch.setattr(
            type(lock.connection_for_test()), "invalidate", _cancel_once, raising=True
        )

        with pytest.raises(asyncio.CancelledError):
            await lock.release(key)

        assert fired == [1], "the injected cancellation never fired"

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
