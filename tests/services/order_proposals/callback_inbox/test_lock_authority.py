"""W5 — the production worker really holds the shipped session lock.

Adversarial review R7, blocker 12. The existing tests prove that a correct
lock *class* exists, and separately that *some* advisory lock is held during
*some* handler. Neither pins the two together: a worker that took a
transaction-scoped lock, or a different short-lived one, or an in-process
lock, would satisfy both.

So this module ties the knot three ways:

* structurally -- ``process_callback_job`` enters the shipped
  ``job_advisory_lock`` / ``PostgresJobAdvisoryLock``, and no
  ``pg_try_advisory_xact_lock`` exists anywhere in the callback-inbox path;
* by instance -- a call-through spy records that the object actually acquired
  is the production class, holding its own ``AsyncConnection`` on its own
  backend;
* behaviourally -- while the **real default core seam** is blocked mid-flight,
  an independent PostgreSQL backend cannot take that exact key, and can once
  the job is terminal.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import pathlib
import uuid
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.timezone import now_kst
from app.services.order_proposals.callback_inbox import locks as locks_module

from .conftest import (
    FakeNotifier,
    held_lock_backend_pid_for_test,
    held_lock_connection_for_test,
    load_job,
    make_update,
    proposal_callback_data,
)

pytestmark = pytest.mark.integration

_PACKAGE = (
    pathlib.Path(__file__).resolve().parents[4]
    / "app/services/order_proposals/callback_inbox"
)


async def _queue(inbox_cleanup: list[uuid.UUID], data: str) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 660_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(data=data, update_id=update_id, callback_id=f"cbq-{update_id}"),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _probe(key: int) -> tuple[bool, int]:
    """Try the key from a genuinely independent backend, and say which."""
    from app.core import db

    connection = await db.engine.connect()
    try:
        pid = int(
            (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        acquired = bool(
            (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"), {"k": key}
                )
            ).scalar_one()
        )
        if acquired:
            await connection.execute(
                text("SELECT pg_advisory_unlock(CAST(:k AS bigint))"), {"k": key}
            )
        await connection.commit()
        return acquired, pid
    finally:
        await connection.close()


# ---------------------------------------------------------------------------
# structural
# ---------------------------------------------------------------------------


def test_the_callback_inbox_never_uses_a_transaction_scoped_advisory_lock() -> None:
    """R7 B12 — an xact lock dies at the worker's own ``processing`` commit.

    Scans *code*, not prose: ``locks.py``'s docstring names the xact form to
    explain why it is wrong, and a substring scan over the whole file would
    flag that explanation as the offence.
    """
    offenders: list[str] = []
    executable_strings = 0
    for path in sorted(_PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings:
                continue
            executable_strings += 1
            for banned in ("pg_try_advisory_xact_lock", "pg_advisory_xact_lock"):
                if banned in node.value:
                    offenders.append(f"{path.name}:{node.lineno}: {banned}")
    assert not offenders, offenders
    assert executable_strings > 20, "the scan found almost no strings to check"

    # ... and the session-scoped pair really is what the lock module executes.
    locks_tree = ast.parse((_PACKAGE / "locks.py").read_text(encoding="utf-8"))
    statements = {
        node.value
        for node in ast.walk(locks_tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert any("pg_try_advisory_lock" in item for item in statements)
    assert any("pg_advisory_unlock" in item for item in statements)


def test_the_worker_enters_the_shipped_lock_context() -> None:
    """The worker names the production lock helper and nothing else."""
    from app.services.order_proposals.callback_inbox import worker as worker_module

    assert worker_module.job_advisory_lock is locks_module.job_advisory_lock

    tree = ast.parse(inspect.getsource(worker_module.process_callback_job))
    entered = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "job_advisory_lock" in entered, sorted(entered)

    # No other locking primitive sneaks in beside it.
    body = inspect.getsource(worker_module)
    for banned in ("asyncio.Lock(", "threading.Lock(", "FileLock", "flock"):
        assert banned not in body, banned


def test_the_production_lock_owns_a_dedicated_connection() -> None:
    """``try_acquire`` checks out its own connection from the engine."""
    source = inspect.getsource(locks_module.PostgresJobAdvisoryLock.try_acquire)
    assert "db.engine.connect()" in source
    # It is stored, so its lifetime is the lock's lifetime.
    assert "self._connection = connection" in source


# ---------------------------------------------------------------------------
# by instance + behaviourally, through the real default core seam
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_shipped_lock_is_held_on_its_own_backend_across_the_real_core(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R7 B12 — the exact production lock, across real core I/O and commits.

    No ``handler=`` override: the default seam runs the real
    ``handle_normalized_callback``. Only the broker leg is a fake, and it
    blocks so a second backend can look at the lock while the core is
    genuinely mid-flight, after the core has already committed at least once.
    """
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    from .conftest import seed_proposal

    group = await seed_proposal(db_session, nonce="lockauth123", symbol="LCKKR")
    job_id = await _queue(inbox_cleanup, proposal_callback_data(group))
    key = job_advisory_lock_key(job_id)

    # Call-through spy: records the real object and its real backend.
    acquired_by: list[tuple[Any, int, AsyncConnection]] = []
    original_try = locks_module.PostgresJobAdvisoryLock.try_acquire

    async def _spy(self, lock_key: int) -> bool:
        got = await original_try(self, lock_key)
        if got:
            acquired_by.append(
                (
                    self,
                    await held_lock_backend_pid_for_test(self),
                    held_lock_connection_for_test(self),
                )
            )
        return got

    monkeypatch.setattr(
        locks_module.PostgresJobAdvisoryLock, "try_acquire", _spy, raising=True
    )

    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)

    inside = asyncio.Event()
    release = asyncio.Event()
    observed: dict[str, Any] = {}

    async def _blocking_broker(*, service, proposal_id, now, **kwargs):
        from app.services.order_proposals.revalidation import RungOutcome

        # The core has already consumed the nonce and taken the commit lease
        # by the time the broker leg runs, so this really is "across core I/O".
        observed["during"] = await _probe(key)
        inside.set()
        await release.wait()
        return [RungOutcome(0, "submitted_acked", {})]

    task = asyncio.create_task(
        process_callback_job(job_id, revalidate_fn=_blocking_broker)
    )
    await asyncio.wait_for(inside.wait(), timeout=20)
    release.set()
    result = await task

    assert result["status"] == "succeeded", result

    # -- instance: it was the shipped class, on its own backend --------------
    assert len(acquired_by) == 1, acquired_by
    holder, holder_pid, connection = acquired_by[0]
    assert type(holder) is locks_module.PostgresJobAdvisoryLock
    assert isinstance(connection, AsyncConnection)

    # -- behaviour: nobody else could take that key mid-core -----------------
    during_acquired, during_pid = observed["during"]
    assert during_acquired is False, "the lock was not held across the real core"
    assert during_pid != holder_pid, "the probe rode the worker's own backend"

    # -- and it is released once the job is terminal -------------------------
    after_acquired, _ = await _probe(key)
    assert after_acquired is True

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
