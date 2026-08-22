"""W5 — the seams production actually uses, not the ones tests inject.

Adversarial review R4. Every other module in this package injects ``handler=``
or ``enqueue_fn=`` or patches ``process_callback_job`` away. That leaves three
things unproven, and all three are the *production* path:

B4  the default worker really does reach the existing callback core, with the
    exact normalized envelope and a processing-time clock -- not some second
    parser or a shortcut that skips the approval gates;
B5  the PostgreSQL advisory lock is really still held **while the handler is
    running**, observed from a different backend -- an implementation that
    released it before the call and relied on an in-process asyncio lock would
    pass every other concurrency test here;
B6  the default Redis producer really serialises one opaque job UUID and
    nothing else.

No live Telegram, broker, Redis or production database is involved: the broker
transport, the notifier and the broker leg are fakes, and the only real network
is the isolated test PostgreSQL.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import time
import uuid
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals import telegram_callback as callback_module

from .conftest import (
    CHAT_ID,
    FakeNotifier,
    load_job,
    make_update,
    proposal_callback_data,
    seed_proposal,
)

pytestmark = pytest.mark.integration


def _synthetic_data(nonce: str = "nonce123456") -> str:
    from app.services.order_proposals.approval_message import build_callback_data
    from app.services.order_proposals.dispatch_contract import (
        ApprovalCardKind,
        DispatchBinding,
        build_membership_digest,
    )

    proposal_id = uuid.uuid4()
    return build_callback_data(
        action="op",
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=uuid.uuid4(),
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
            ),
        ),
    )


async def _queue(
    inbox_cleanup: list[uuid.UUID], *, data: str | None = None
) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 970_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=data or _synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _expire(proposal_id: uuid.UUID) -> None:
    """Push ``valid_until`` into the past, as queue delay would in production."""
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review.order_proposals SET valid_until = now() "
                "- interval '5 minutes' WHERE proposal_id = :pid"
            ),
            {"pid": proposal_id},
        )
        await session.commit()


# ---------------------------------------------------------------------------
# B4 — the default worker seam reaches the real callback core
# ---------------------------------------------------------------------------


def test_the_worker_names_exactly_one_callback_core() -> None:
    """Pin the import path, and prove no second parser/dispatch exists here."""
    from app.services.order_proposals.callback_inbox import worker as worker_module

    assert (
        worker_module.handle_normalized_callback
        is callback_module.handle_normalized_callback
    )
    # The default must be resolved by name at call time, not frozen into the
    # signature -- otherwise the production seam is invisible and untestable.
    assert (
        inspect.signature(worker_module.process_callback_job)
        .parameters["handler"]
        .default
        is None
    )

    source = inspect.getsource(worker_module)
    for alternate in (
        "parse_callback_data",
        "handle_callback_update",
        "revalidate_and_submit",
        "consume_published_proposal_callback",
        "preflight_published_proposal_callback",
        "acquire_commit_lease",
    ):
        assert alternate not in source, (
            f"the worker names {alternate!r} directly; the callback core owns it"
        )


@pytest.mark.asyncio
async def test_the_default_task_delegates_to_the_real_core_with_exact_fields(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 B4 — run the real TaskIQ task with no handler override.

    The spy *calls through* to the untouched callback core, so every approval
    gate still executes; it exists only to record what the worker handed it.
    """
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )

    group = await seed_proposal(db_session, nonce="prodseam123", symbol="PRDKR")
    job_id = await _queue(inbox_cleanup, data=proposal_callback_data(group))
    await _expire(group.proposal_id)

    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)

    seen: list[tuple[Any, dict[str, Any]]] = []
    real_core = callback_module.handle_normalized_callback

    async def _spy(normalized, **kwargs):
        seen.append((normalized, kwargs))
        return await real_core(normalized, **kwargs)

    monkeypatch.setattr(worker_module, "handle_normalized_callback", _spy)

    result = await task_module.run_telegram_callback_job(str(job_id))

    # -- it went through the real core, exactly once --------------------------
    assert len(seen) == 1, seen
    normalized, kwargs = seen[0]

    # -- with the exact normalized envelope, rebuilt from the stored row ------
    assert normalized.chat_id == CHAT_ID
    assert normalized.chat_id_key == str(CHAT_ID)
    assert normalized.message_id == 555
    assert normalized.callback == callback_module.parse_callback_data(
        proposal_callback_data(group)
    )

    # -- and a processing-time clock, not the receive time --------------------
    assert kwargs["now_fn"] is now_kst, "the worker did not pass the live clock"
    row = await load_job(job_id)
    assert row is not None
    assert kwargs["now"] >= row.received_at
    assert kwargs["now"].tzinfo is not None

    # -- the real fail-closed gate fired: expired, and it says so in Korean ---
    assert result == {"status": "discarded", "job_id": str(job_id)}
    assert row.state == "discarded"
    assert row.outcome == "expired"
    assert any("제안 만료" in edit[2] for edit in notifier.edited), notifier.edited

    # -- and the approval was not spent ---------------------------------------
    async with AsyncSessionLocal() as session:
        refreshed, _ = await OrderProposalsService(session).get_proposal(
            group.proposal_id
        )
        assert refreshed.approval_nonce_used_at is None


@pytest.mark.asyncio
async def test_the_default_seam_fails_closed_on_a_replayed_binding(
    _bootstrap_test_schema,
    db_session,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R4 B4 — a second representative gate, through the same default seam.

    No handler override at all this time. A consumed nonce must stop at the
    published-binding preflight, which by contract makes *no* external call --
    so an empty fake notifier is itself the assertion.
    """
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.tasks import telegram_callback_inbox_tasks as task_module

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        True,
        raising=False,
    )

    group = await seed_proposal(db_session, nonce="prodreplay1", symbol="PRPKR")
    job_id = await _queue(inbox_cleanup, data=proposal_callback_data(group))

    # Someone already used this approval.
    async with AsyncSessionLocal() as session:
        await session.execute(
            text(
                "UPDATE review.order_proposals SET approval_nonce_used_at = now() "
                "WHERE proposal_id = :pid"
            ),
            {"pid": group.proposal_id},
        )
        await session.commit()

    notifier = FakeNotifier()
    monkeypatch.setattr(worker_module, "resolve_notifier", lambda: notifier)

    result = await task_module.run_telegram_callback_job(str(job_id))

    assert result == {"status": "discarded", "job_id": str(job_id)}
    row = await load_job(job_id)
    assert row is not None
    assert row.outcome == "nonce_replay"
    assert notifier.external_calls == 0, (
        "a stale binding reached an external call before failing closed"
    )


# ---------------------------------------------------------------------------
# B5 — the lock is held across the handler, observed from another backend
# ---------------------------------------------------------------------------


async def _independent_backend_can_take(key: int) -> tuple[bool, int]:
    """Try the key from a genuinely separate PostgreSQL backend."""
    from app.core import db

    connection = await db.engine.connect()
    try:
        pid = int(
            (await connection.execute(text("SELECT pg_backend_pid()"))).scalar_one()
        )
        acquired = bool(
            (
                await connection.execute(
                    text("SELECT pg_try_advisory_lock(CAST(:k AS bigint))"),
                    {"k": key},
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


@pytest.mark.asyncio
async def test_the_pg_lock_is_still_held_while_the_handler_runs(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R4 B5 — cross-backend proof, taken from inside the handler.

    An implementation that released the PostgreSQL lock before invoking the
    handler and relied on an in-process ``asyncio.Lock`` would satisfy
    ``test_two_concurrent_tasks_for_one_job_invoke_the_handler_once`` and fail
    here, which is the point: a second *process* is what the durable inbox
    actually has to exclude.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)
    key = job_advisory_lock_key(job_id)

    inside = asyncio.Event()
    release = asyncio.Event()
    observed: dict[str, Any] = {}

    async def _handler(normalized, **kwargs):
        # Taken from a different backend while this handler is mid-flight.
        observed["during"] = await _independent_backend_can_take(key)
        inside.set()
        await release.wait()
        return {"handled": True, "reason": "approved"}

    task = asyncio.create_task(process_callback_job(job_id, handler=_handler))
    await asyncio.wait_for(inside.wait(), timeout=15)

    # A second worker cannot even start while the first is inside the core.
    second_calls: list[int] = []

    async def _must_not_run(normalized, **kwargs):  # pragma: no cover
        second_calls.append(1)
        return {"handled": True}

    second = await process_callback_job(job_id, handler=_must_not_run)
    assert second["status"] == "lock_contended"
    assert second_calls == []

    release.set()
    result = await task
    assert result["status"] == "succeeded"

    during_acquired, _ = observed["during"]
    # Session advisory locks are re-entrant on the holder backend.  A false
    # try-lock result therefore proves that this probe used a different
    # backend and observed the worker's lock while the handler was running.
    assert during_acquired is False, "the lock was not held across the handler"

    after_acquired, _ = await _independent_backend_can_take(key)
    assert after_acquired is True, "the lock outlived the job"

    row = await load_job(job_id)
    assert row is not None
    assert row.state in {"succeeded", "discarded"}


@pytest.mark.asyncio
async def test_cancelling_a_worker_mid_handler_frees_the_key(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The same seam, cancelled: no orphan lock on a pooled backend."""
    from app.services.order_proposals.callback_inbox.contracts import (
        job_advisory_lock_key,
    )
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)
    key = job_advisory_lock_key(job_id)
    inside = asyncio.Event()

    async def _handler(normalized, **kwargs):
        inside.set()
        await asyncio.sleep(60)
        return {"handled": True}

    task = asyncio.create_task(process_callback_job(job_id, handler=_handler))
    await asyncio.wait_for(inside.wait(), timeout=15)
    assert (await _independent_backend_can_take(key))[0] is False

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert (await _independent_backend_can_take(key))[0] is True


# ---------------------------------------------------------------------------
# B6 — the default Redis producer serialises one opaque UUID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_default_producer_serialises_only_a_job_uuid(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R4 B6 — no ``enqueue_fn`` override; a fake transport under the broker.

    ``kiq`` serialises through the broker's own formatter and calls
    ``broker.kick``, so intercepting ``kick`` captures exactly the bytes a real
    Redis would have received.
    """
    from app.core.config import settings
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    sent: list[Any] = []

    async def _fake_transport(message):
        sent.append(message)

    monkeypatch.setattr(broker, "kick", _fake_transport)

    # Deliberately long and distinctive. Short ids like "42" collide with any
    # random UUID, which would make a substring scan meaningless.
    chat_id = -1009998887776
    user_id = 4242424242424
    message_id = 987654321
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        str(chat_id),
        raising=False,
    )

    nonce = "kicknonce12"
    update_id = 980_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-sentinel-{update_id}"
    update = make_update(
        data=_synthetic_data(nonce),
        update_id=update_id,
        callback_id=callback_id,
        chat_id=chat_id,
        user_id=user_id,
        message_id=message_id,
    )

    result = await ingest_callback_update(update, now=now_kst())
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    assert result.enqueued is True

    assert len(sent) == 1, sent
    raw = sent[0].message.decode()
    payload = json.loads(raw)

    # The complete shape, not just a spot check: any extra field would be a
    # new channel for something to travel on.
    assert set(payload) == {
        "task_id",
        "task_name",
        "labels",
        "labels_types",
        "args",
        "kwargs",
    }, payload
    assert payload["task_name"] == "order_proposals.telegram_callback_job"
    assert payload["args"] == [str(result.job_id)]
    assert payload["kwargs"] == {}
    assert payload["labels"] == {}

    # And nothing from the update appears anywhere in the serialized bytes.
    for leaked in (
        nonce,
        callback_id,
        str(chat_id),
        str(user_id),
        str(message_id),
        str(update_id),
        update["callback_query"]["data"],
        "callback_query",
        "chat",
        "message_id",
        "nonce",
        "digest",
    ):
        assert leaked not in raw, f"the queue payload leaked {leaked!r}: {raw}"


@pytest.mark.asyncio
async def test_a_broker_failure_leaves_the_row_pending_and_still_acks(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R4 B6 — commit succeeded, the real producer failed: still a durable ACK."""
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    async def _dead_transport(message):
        raise ConnectionError("redis is gone")

    monkeypatch.setattr(broker, "kick", _dead_transport)

    update_id = 990_000 + uuid.uuid4().int % 10_000
    result = await ingest_callback_update(
        make_update(
            data=_synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    assert result.accepted is True
    assert result.enqueued is False
    row = await load_job(result.job_id)
    assert row is not None
    assert row.state == "pending"


@pytest.mark.asyncio
async def test_a_failed_commit_never_reaches_the_real_producer(
    _bootstrap_test_schema, monkeypatch
) -> None:
    """R4 B6 — enqueue count is 0 when the row did not commit."""
    import contextlib

    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        CallbackInboxUnavailable,
        ingest_callback_update,
    )

    sent: list[Any] = []

    async def _fake_transport(message):  # pragma: no cover - must not run
        sent.append(message)

    monkeypatch.setattr(broker, "kick", _fake_transport)

    class _ExplodingSession:
        def add(self, *args, **kwargs) -> None:
            return None

        async def execute(self, *args, **kwargs):
            raise RuntimeError("db down")

        async def flush(self, *args, **kwargs):
            raise RuntimeError("db down")

        async def commit(self) -> None:
            raise RuntimeError("db down")

        async def rollback(self) -> None:
            return None

    @contextlib.asynccontextmanager
    async def _factory():
        yield _ExplodingSession()

    with pytest.raises(CallbackInboxUnavailable):
        await ingest_callback_update(
            make_update(data=_synthetic_data(), update_id=991_001),
            now=now_kst(),
            session_factory=_factory,
        )
    assert sent == []


@pytest.mark.asyncio
async def test_a_rejected_update_never_reaches_the_real_producer(
    _bootstrap_test_schema, monkeypatch
) -> None:
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    sent: list[Any] = []

    async def _fake_transport(message):  # pragma: no cover - must not run
        sent.append(message)

    monkeypatch.setattr(broker, "kick", _fake_transport)

    for update in (
        {"update_id": 992_001},
        {
            "update_id": 992_002,
            "callback_query": {
                "id": "cbq-x",
                "from": {"id": 777},
                "message": {"chat": {"id": 999999}, "message_id": 5},
                "data": _synthetic_data(),
            },
        },
    ):
        result = await ingest_callback_update(update, now=now_kst())
        assert result.accepted is False
    assert sent == []


@pytest.mark.asyncio
async def test_default_kiq_producer_ack_deadline_survives_cancellation_resistance(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R33 — the real ``.kiq`` seam cannot hold a webhook ACK hostage.

    This intentionally replaces the old implementation pin on
    ``asyncio.wait_for``.  The fake is below the TaskIQ formatter, so the
    default producer still serialises and calls ``.kiq`` exactly as production
    does; it simply keeps running after the deadline's cancellation request.
    """

    from app.core.config import settings
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    configured = 0.02
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS",
        configured,
        raising=False,
    )

    started = asyncio.Event()
    cancel_requested = asyncio.Event()
    release = asyncio.Event()
    late_failure_seen = asyncio.Event()
    sent_job_ids: list[uuid.UUID] = []
    producer_started_at = 0.0
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    def _capture_loop_error(
        _loop: asyncio.AbstractEventLoop, context: dict[str, object]
    ) -> None:
        loop_errors.append(context)

    loop.set_exception_handler(_capture_loop_error)

    async def _resistant_transport(message: Any) -> None:
        nonlocal producer_started_at
        payload = json.loads(message.message.decode())
        sent_job_ids.append(uuid.UUID(payload["args"][0]))
        producer_started_at = time.monotonic()
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancel_requested.set()
        late_failure_seen.set()
        raise RuntimeError("late broker enqueue failure")

    monkeypatch.setattr(broker, "kick", _resistant_transport)

    update_id = 994_000 + uuid.uuid4().int % 10_000
    request = asyncio.create_task(
        ingest_callback_update(
            make_update(
                data=_synthetic_data(),
                update_id=update_id,
                callback_id=f"cbq-{update_id}",
            ),
            now=now_kst(),
        )
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=2.0)
        result = await asyncio.wait_for(asyncio.shield(request), timeout=0.15)
        elapsed = time.monotonic() - producer_started_at
        assert result.job_id is not None
        inbox_cleanup.append(result.job_id)

        assert result.accepted is True
        assert result.enqueued is False
        assert cancel_requested.is_set()
        assert elapsed < 0.15, elapsed

        row = await load_job(result.job_id)
        assert row is not None
        assert row.state == "pending"
    finally:
        release.set()
        try:
            await asyncio.wait_for(request, timeout=1.0)
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        for job_id in sent_job_ids:
            if job_id not in inbox_cleanup:
                inbox_cleanup.append(job_id)

    assert late_failure_seen.is_set()
    assert not [
        context
        for context in loop_errors
        if "Task exception was never retrieved" in str(context.get("message", ""))
    ], loop_errors


@pytest.mark.asyncio
async def test_the_default_producer_gives_up_at_the_configured_timeout(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R24 — and the real `.kiq` path really stops there, deterministically."""
    from app.core.config import settings
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    configured = 0.3
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS",
        configured,
        raising=False,
    )

    entered = asyncio.Event()

    async def _hangs(message):
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(broker, "kick", _hangs)

    update_id = 996_000 + uuid.uuid4().int % 10_000
    began = time.monotonic()
    result = await ingest_callback_update(
        make_update(
            data=_synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
    )
    elapsed = time.monotonic() - began
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    assert entered.is_set(), "the real producer was never reached"
    assert result.accepted is True
    assert result.enqueued is False
    assert configured <= elapsed < configured + 1.0, elapsed

    row = await load_job(result.job_id)
    assert row is not None
    assert row.state == "pending"


@pytest.mark.asyncio
async def test_the_enqueue_timeout_is_bounded_by_the_setting(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """A hung broker must not hold the webhook thread for the whole timeout."""
    from app.core.config import settings
    from app.core.taskiq_broker import broker
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    async def _hanging_transport(message):
        await asyncio.sleep(60)

    monkeypatch.setattr(broker, "kick", _hanging_transport)
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS",
        0.05,
        raising=False,
    )

    update_id = 993_000 + uuid.uuid4().int % 10_000
    started = now_kst()
    result = await ingest_callback_update(
        make_update(
            data=_synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=started,
    )
    elapsed = now_kst() - started
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    assert result.accepted is True
    assert result.enqueued is False
    assert elapsed < timedelta(seconds=10), elapsed
