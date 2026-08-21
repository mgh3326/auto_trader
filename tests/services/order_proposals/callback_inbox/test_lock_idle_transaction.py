"""W5 — the lock holder must not sit inside an open transaction.

Adversarial review R30. ``try_acquire`` ran ``pg_try_advisory_lock`` and never
committed, so the dedicated connection stayed inside that implicit
transaction for the entire handler -- every broker call, every core commit,
however long the job took. ``pg_stat_activity`` showed the holder as ``idle in
transaction`` with an open ``xact_start`` the whole time.

That is not merely untidy. If ``idle_in_transaction_session_timeout`` is
shorter than a job, PostgreSQL terminates the backend, and terminating the
backend releases every advisory lock it held -- silently, while the coroutine
that thinks it owns the job keeps running. Same-job exclusion would be gone
with nothing in the application noticing. Even with the timeout disabled, a
long-lived idle transaction holds back xmin and is a standing operational
cost.

A session-level advisory lock survives ``COMMIT``; that is the whole reason
this module uses the session-scoped form. So the transaction can be closed
immediately and the lock still held until the unlock or the backend dies.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from sqlalchemy import text

from app.services.order_proposals.callback_inbox import locks as locks_module

pytestmark = pytest.mark.integration


async def _holder_activity(observer, key: int) -> dict:
    """Read the holder's session state without touching its connection.

    Going through ``pg_locks`` rather than asking the holder is the point:
    running any statement on it would open a fresh transaction and destroy
    the very thing under test.
    """
    row = (
        await observer.execute(
            text(
                "SELECT a.pid, a.state, a.xact_start, a.backend_xid "
                "FROM pg_locks l JOIN pg_stat_activity a ON a.pid = l.pid "
                "WHERE l.locktype = 'advisory' AND l.classid = 0 "
                "AND l.objid = :key AND l.objsubid = 1"
            ),
            {"key": key},
        )
    ).mappings()
    rows = row.all()
    assert len(rows) == 1, f"expected exactly one advisory lock holder, got {rows}"
    return dict(rows[0])


@pytest.mark.asyncio
async def test_the_holder_is_idle_not_idle_in_transaction(
    _bootstrap_test_schema, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R30 — the transaction is closed the moment the lock is taken."""
    from app.core import db

    # Small and positive so it lands in ``pg_locks`` as classid 0 / objid key.
    key = int(uuid.uuid4().int % 1_000_000_000) + 1

    real_engine = db.engine
    observer = await real_engine.connect()
    captured: list = []

    class _Capturing:
        """Hands out the real connection and keeps a reference to it.

        The test needs to look at the holder's connection, and the runtime
        exposes ``connection_for_test`` for that -- but that method exists
        only for tests and is on the list for removal (R31). Capturing it at
        the engine keeps this test independent of that decision.

        Bound to ``real_engine`` rather than reading ``db.engine`` at call
        time, which would find this class and recurse.
        """

        async def connect(self):
            connection = await real_engine.connect()
            captured.append(connection)
            return connection

    monkeypatch.setattr(db, "engine", _Capturing(), raising=True)
    lock = locks_module.PostgresJobAdvisoryLock()
    try:
        assert await lock.try_acquire(key) is True
        assert len(captured) == 1

        # Client side: nothing open.
        assert captured[0].in_transaction() is False, (
            "the lock holder is still inside a transaction"
        )

        # Server side: the backend really is idle, with no open transaction.
        activity = await _holder_activity(observer, key)
        assert activity["state"] == "idle", activity["state"]
        assert activity["xact_start"] is None, activity["xact_start"]
        assert activity["backend_xid"] is None, activity["backend_xid"]

        # ... and the lock is still held, which is the point of committing
        # rather than rolling back or closing.
        taken = bool(
            (
                await observer.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        assert taken is False, "committing released the lock"

        # A different job is unaffected.
        other = key + 1
        free = bool(
            (
                await observer.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"),
                    {"k": other},
                )
            ).scalar_one()
        )
        assert free is True
        await observer.execute(
            text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": other}
        )
        await observer.commit()

        await lock.release(key)

        # Released cleanly, so it can be taken again.
        retaken = bool(
            (
                await observer.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        assert retaken is True
        await observer.execute(
            text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": key}
        )
        await observer.commit()
    finally:
        await observer.close()


@pytest.mark.asyncio
async def test_the_holder_stays_idle_across_a_handler(_bootstrap_test_schema) -> None:
    """R30 — and it is still idle later, not just at the instant of acquiring."""
    from app.core import db
    from app.services.order_proposals.callback_inbox.locks import job_advisory_lock

    key = int(uuid.uuid4().int % 1_000_000_000) + 1
    observer = await db.engine.connect()
    try:
        async with job_advisory_lock(key) as acquired:
            assert acquired is True
            # Whatever the handler is doing, the holder is not in a
            # transaction while it does it.
            for _ in range(3):
                await asyncio.sleep(0)
                activity = await _holder_activity(observer, key)
                assert activity["state"] == "idle", activity["state"]
                assert activity["xact_start"] is None
    finally:
        await observer.close()


# ---------------------------------------------------------------------------
# the commit is inside the acquire's own failure boundary
# ---------------------------------------------------------------------------
#
# A commit failure is an ambiguous acquire like any other: the server may hold
# the lock even though the client lost the transaction. So it has to terminate
# rather than pool, the handler must not run, and -- because the cleanup can
# itself be cancelled -- the cleanup has to finish before anything else does
# (R27, R27d).


class _CommitFails:
    """Acquires the lock, then loses the connection at the commit.

    ``invalidate`` blocks at a gate so a test can cancel the *outer* task
    while the retained cleanup is genuinely mid-flight. A cleanup that can be
    interrupted there is the exact failure R27 closed, and a fake that raises
    immediately cannot tell the difference.
    """

    def __init__(
        self, *, error: BaseException, terminates: bool = True, gated: bool = False
    ) -> None:
        self.events: list[str] = []
        self.at_gate = asyncio.Event()
        self.gate = asyncio.Event()
        self._error = error
        self._terminates = terminates
        self._gated = gated
        events = self.events

        class _Driver:
            @staticmethod
            def terminate() -> None:
                events.append("driver_terminate")
                if not terminates:
                    raise OSError("driver terminate failed")

        class _Raw:
            driver_connection = _Driver()

            @staticmethod
            def detach() -> None:
                events.append("detach")

            @staticmethod
            def invalidate() -> None:
                events.append("raw_invalidate")

        self._raw = _Raw()

    async def get_raw_connection(self):
        self.events.append("get_raw_connection")
        return self._raw

    async def execute(self, *_args, **_kwargs):
        self.events.append("execute")

        class _Result:
            @staticmethod
            def scalar_one():
                return True

        return _Result()

    async def commit(self) -> None:
        self.events.append("commit_attempted")
        raise self._error

    async def invalidate(self) -> None:
        self.events.append("invalidate_started")
        if self._gated:
            self.at_gate.set()
            await self.gate.wait()
        if not self._terminates:
            raise OSError("cannot invalidate")
        self.events.append("invalidate_completed")

    async def close(self) -> None:
        self.events.append("close")


def _engine_returning(connection):
    class _Engine:
        async def connect(self):
            return connection

    return _Engine()


async def _acquire_under_gate(connection, key: int, cancels: int):
    """Run one acquire, cancel the outer task ``cancels`` times mid-cleanup."""
    from app.services.order_proposals.callback_inbox.locks import job_advisory_lock

    entered: list[int] = []

    async def _body() -> None:
        async with job_advisory_lock(key) as acquired:  # pragma: no cover
            entered.append(1 if acquired else 0)

    task = asyncio.create_task(_body())
    await asyncio.wait_for(connection.at_gate.wait(), timeout=5)
    for _ in range(cancels):
        task.cancel()
        await asyncio.sleep(0)
    assert task.done() is False, "a cancellation abandoned the cleanup"
    assert "invalidate_completed" not in connection.events
    connection.gate.set()
    return task, entered


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_commit_discards_the_lock_and_blocks_the_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R30 — the commit shares the acquire's failure boundary.

    Committing outside the ``try`` -- or after ``self._connection`` is set --
    would open a window in which the lock is held, the transaction is broken,
    and the handler runs anyway.
    """
    from app.core import db
    from app.services.order_proposals.callback_inbox.locks import job_advisory_lock

    connection = _CommitFails(error=OSError("commit failed"))
    monkeypatch.setattr(db, "engine", _engine_returning(connection), raising=True)

    entered: list[int] = []
    with pytest.raises(OSError):
        async with job_advisory_lock(4321) as acquired:  # pragma: no cover
            entered.append(1 if acquired else 0)

    assert entered == [], "the handler ran on a lock whose commit failed"
    assert "invalidate_completed" in connection.events, connection.events
    assert "close" in connection.events, connection.events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cancelled_commit_re_raises_that_cancellation_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R30 x R27 — gated cleanup, cancelled twice, original identity preserved."""
    from app.core import db

    original = asyncio.CancelledError("the-commit-cancellation")
    connection = _CommitFails(error=original, gated=True)
    monkeypatch.setattr(db, "engine", _engine_returning(connection), raising=True)

    task, entered = await _acquire_under_gate(connection, 4321, cancels=2)

    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert raised.value is original, "a later cancellation displaced the first"
    assert raised.value.args == ("the-commit-cancellation",)
    assert entered == [], "the handler ran on a lock whose commit was cancelled"
    # Re-raised only after the cleanup genuinely finished.
    assert "invalidate_completed" in connection.events, connection.events
    assert "close" in connection.events, connection.events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_cancelled_cleanup_carries_the_commit_failure_as_its_cause(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R30 x R27 — a non-cancellation original must not be lost either."""
    from app.core import db

    original = RuntimeError("commit failed")
    connection = _CommitFails(error=original, gated=True)
    monkeypatch.setattr(db, "engine", _engine_returning(connection), raising=True)

    task, entered = await _acquire_under_gate(connection, 4321, cancels=2)

    with pytest.raises(asyncio.CancelledError) as raised:
        await task

    assert raised.value.__cause__ is original, (
        "the commit failure was dropped when the cleanup was cancelled"
    )
    assert entered == []
    assert "invalidate_completed" in connection.events, connection.events
    assert "close" in connection.events, connection.events


@pytest.mark.unit
@pytest.mark.asyncio
async def test_an_unterminable_commit_failure_quarantines_the_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R30 x R27d — unproven termination still means no close and no checkin."""
    from app.core import db
    from app.services.order_proposals.callback_inbox.locks import (
        LockTerminationUnproven,
        job_advisory_lock,
    )

    connection = _CommitFails(error=OSError("commit failed"), terminates=False)
    monkeypatch.setattr(db, "engine", _engine_returning(connection), raising=True)

    entered: list[int] = []
    with pytest.raises(LockTerminationUnproven):
        async with job_advisory_lock(4321) as acquired:  # pragma: no cover
            entered.append(1 if acquired else 0)

    assert entered == [], "the handler ran on a backend we could not terminate"
    assert "close" not in connection.events, connection.events
    assert "raw_invalidate" not in connection.events, connection.events
    # Module-private on purpose: the accessor is a test-only surface on the
    # runtime and is on the list for removal (R31).
    assert connection in locks_module._QUARANTINE  # noqa: SLF001
