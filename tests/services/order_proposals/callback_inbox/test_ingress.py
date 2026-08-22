"""W5 — durable ingress: normalize, persist, dedupe, best-effort kick.

RED-before-fix items 2, 3 and 4, against the real run-owned database.

The contract this file pins:

* the row is **committed** before the caller is told it was accepted;
* a duplicate Telegram delivery creates exactly one row;
* a DB failure raises ``CallbackInboxUnavailable`` and never attempts the
  Redis kick;
* a hung or failing kick neither rolls the row back nor blocks the caller.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals.approval_message import build_callback_data
from app.services.order_proposals.dispatch_contract import (
    ApprovalCardKind,
    DispatchBinding,
    build_membership_digest,
)

from .conftest import CHAT_ID, load_job, make_update

pytestmark = pytest.mark.integration


def _valid_callback_data(*, action: str = "op", nonce: str = "nonce123456") -> str:
    proposal_id = uuid.uuid4()
    attempt_id = uuid.uuid4()
    return build_callback_data(
        action=action,
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=attempt_id,
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=1,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=1,
                members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
            ),
        ),
    )


async def _count_rows(update_digest: str) -> int:
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    async with AsyncSessionLocal() as session:
        return (
            await session.execute(
                select(func.count())
                .select_from(TelegramCallbackInboxJob)
                .where(TelegramCallbackInboxJob.update_digest == update_digest)
            )
        ).scalar_one()


@pytest.mark.asyncio
async def test_a_valid_callback_is_committed_and_kicked_once(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 700_000 + uuid.uuid4().int % 10_000
    update = make_update(
        data=_valid_callback_data(), update_id=update_id, callback_id=f"cbq-{update_id}"
    )
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    result = await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    assert result.accepted is True
    assert result.duplicate is False
    assert result.reason == "queued"
    assert result.enqueued is True
    assert kicked == [result.job_id]

    digest = build_update_digest(
        update_id=update_id, callback_query_id=f"cbq-{update_id}"
    )
    assert await _count_rows(digest) == 1

    # Committed, therefore visible from a brand-new session.
    row = await load_job(result.job_id)
    assert row is not None
    assert row.state == "pending"
    assert row.attempt_count == 0
    assert row.chat_id == str(CHAT_ID)


@pytest.mark.asyncio
async def test_duplicate_delivery_creates_exactly_one_row(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 4, ingress half — Telegram retries the same ``update_id``."""
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 710_000 + uuid.uuid4().int % 10_000
    update = make_update(
        data=_valid_callback_data(), update_id=update_id, callback_id=f"cbq-{update_id}"
    )

    async def _enqueue(job_id: uuid.UUID) -> None:
        return None

    first = await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
    second = await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
    assert first.job_id is not None
    inbox_cleanup.append(first.job_id)

    assert first.duplicate is False
    assert second.duplicate is True
    assert second.job_id == first.job_id
    assert second.reason == "duplicate"

    digest = build_update_digest(
        update_id=update_id, callback_query_id=f"cbq-{update_id}"
    )
    assert await _count_rows(digest) == 1


@pytest.mark.asyncio
async def test_a_reused_delivery_id_with_a_different_envelope_fails_closed(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R2 — same delivery identity, different binding, is not a duplicate.

    Only an identical re-delivery is a benign duplicate. A tampered envelope
    reusing the delivery id must neither overwrite the stored row nor be
    accepted as "already queued" — it is dropped, unqueued, fail-closed.
    """
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 760_000 + uuid.uuid4().int % 10_000
    original_data = _valid_callback_data(nonce="nonce123456")
    tampered_data = _valid_callback_data(nonce="tampered999")

    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    first = await ingest_callback_update(
        make_update(
            data=original_data,
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_enqueue,
    )
    assert first.job_id is not None
    inbox_cleanup.append(first.job_id)

    tampered = await ingest_callback_update(
        make_update(
            data=tampered_data,
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_enqueue,
    )

    assert tampered.accepted is False
    assert tampered.duplicate is False
    assert tampered.reason == "delivery_conflict"
    assert tampered.enqueued is False
    assert kicked == [first.job_id], "a conflicting delivery was queued"

    row = await load_job(first.job_id)
    assert row is not None
    assert row.nonce == "nonce123456", "the stored envelope was overwritten"


@pytest.mark.asyncio
async def test_the_row_is_visible_to_an_independent_session_before_the_kick(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R2 — commit-before-kick proved by visibility, not by call ordering.

    The enqueue callback opens a brand-new session. If the insert were still
    inside an uncommitted transaction, that session would see nothing.
    """
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 770_000 + uuid.uuid4().int % 10_000
    seen: list[str | None] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        row = await load_job(job_id)
        seen.append(None if row is None else row.state)

    result = await ingest_callback_update(
        make_update(
            data=_valid_callback_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_enqueue,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    assert seen == ["pending"], "the kick fired before the row was committed"


@pytest.mark.asyncio
async def test_concurrent_duplicate_deliveries_still_create_one_row(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 720_000 + uuid.uuid4().int % 10_000
    update = make_update(
        data=_valid_callback_data(), update_id=update_id, callback_id=f"cbq-{update_id}"
    )

    async def _enqueue(job_id: uuid.UUID) -> None:
        return None

    results = await asyncio.gather(
        *(
            ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
            for _ in range(4)
        )
    )
    job_ids = {r.job_id for r in results}
    assert len(job_ids) == 1
    job_id = job_ids.pop()
    assert job_id is not None
    inbox_cleanup.append(job_id)
    assert sum(1 for r in results if not r.duplicate) == 1

    digest = build_update_digest(
        update_id=update_id, callback_query_id=f"cbq-{update_id}"
    )
    assert await _count_rows(digest) == 1


#: Every field of the normalized envelope this row persists. Each is raced
#: on its own: the two deliveries share a delivery identity and differ in
#: exactly one place, so an implementation that compares only some of them
#: (or only the nonce) lets a different call through as a "duplicate".
CONFLICTING_FIELDS = (
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
    "chat_id",
    "telegram_user_id",
    "message_id",
)


def _envelope_variant(field: str | None) -> dict[str, Any]:
    """Build one delivery, optionally differing in exactly one field."""
    from app.services.order_proposals.dispatch_contract import build_membership_digest

    proposal_id = uuid.UUID("22222222-3333-4444-8555-666666666666")
    attempt_id = uuid.UUID("77777777-8888-4999-8aaa-bbbbbbbbbbbb")
    action = "op"
    nonce = "baselinenon"
    revision = 1
    chat_id = CHAT_ID
    user_id = 777
    message_id = 555
    digest_members = [{"proposal_id": str(proposal_id), "approval_nonce": nonce}]

    if field == "action":
        action = "dn"
    elif field == "subject_short":
        proposal_id = uuid.UUID("99999999-3333-4444-8555-666666666666")
        digest_members = [{"proposal_id": str(proposal_id), "approval_nonce": nonce}]
    elif field == "dispatch_attempt_id":
        attempt_id = uuid.UUID("cccccccc-8888-4999-8aaa-bbbbbbbbbbbb")
    elif field == "membership_revision":
        revision = 2
    elif field == "membership_digest":
        digest_members = [{"proposal_id": str(proposal_id), "approval_nonce": "other"}]
    elif field == "nonce":
        nonce = "variantnonc"
        digest_members = [{"proposal_id": str(proposal_id), "approval_nonce": nonce}]
    elif field == "chat_id":
        chat_id = CHAT_ID + 1
    elif field == "telegram_user_id":
        user_id = 888
    elif field == "message_id":
        message_id = 556

    data = build_callback_data(
        action=action,
        proposal_id=proposal_id,
        nonce=nonce,
        binding=DispatchBinding(
            attempt_id=attempt_id,
            card_kind=ApprovalCardKind.MANUAL,
            membership_revision=revision,
            membership_digest=build_membership_digest(
                card_kind=ApprovalCardKind.MANUAL,
                membership_revision=revision,
                members=digest_members,
            ),
        ),
    )
    return {
        "data": data,
        "chat_id": chat_id,
        "user_id": user_id,
        "message_id": message_id,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("field", CONFLICTING_FIELDS)
async def test_a_concurrent_conflicting_delivery_loses_and_fails_closed(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    """R7 B13 — two different calls, one delivery identity, one field apart.

    Two independent sessions meet at the **repository insert boundary**, not
    merely before ``ingest_callback_update``, so a process-global lock in front
    of the ingress cannot serialise them into a benign sequence. One wins at
    the unique index; the loser reaches the conflict path with a genuinely
    different envelope and must fail closed.

    Racing one field at a time is what makes a partial comparison visible: an
    implementation that checks only the nonce (or only the binding, and not
    the Telegram identifiers this row persists) passes the other parameters
    and fails exactly the field it forgot.
    """
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR",
        f"{CHAT_ID},{CHAT_ID + 1}",
        raising=False,
    )

    update_id = 790_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-{update_id}"
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    # The barrier sits inside the repository, immediately before the INSERT
    # is flushed, so both sessions are already open and race at PostgreSQL.
    barrier = asyncio.Barrier(2)
    original_insert = repo_module.CallbackInboxRepository.insert

    async def _barriered_insert(self, **fields: Any):
        await barrier.wait()
        return await original_insert(self, **fields)

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository, "insert", _barriered_insert, raising=True
    )

    async def _attempt(variant: dict[str, Any]):
        return await ingest_callback_update(
            make_update(
                data=variant["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )

    baseline, variant = await asyncio.gather(
        _attempt(_envelope_variant(None)), _attempt(_envelope_variant(field))
    )

    accepted = [result for result in (baseline, variant) if result.accepted]
    conflicted = [
        result for result in (baseline, variant) if result.reason == "delivery_conflict"
    ]
    assert len(accepted) == 1, (field, baseline, variant)
    assert len(conflicted) == 1, (
        f"{field}: a different envelope was not recognised as a conflict "
        f"({baseline.reason} / {variant.reason})"
    )

    winner = accepted[0]
    assert winner.job_id is not None
    inbox_cleanup.append(winner.job_id)
    assert winner.duplicate is False
    assert winner.reason == "queued"

    loser = conflicted[0]
    assert loser.accepted is False
    assert loser.duplicate is False, f"{field}: a different envelope is not a duplicate"
    assert loser.job_id is None
    assert loser.enqueued is False

    assert kicked == [winner.job_id], (field, kicked)
    digest = build_update_digest(update_id=update_id, callback_query_id=callback_id)
    assert await _count_rows(digest) == 1


@pytest.mark.asyncio
async def test_an_identical_concurrent_redelivery_is_still_a_duplicate(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: conflict handling must not reject a genuine retry."""
    from app.services.order_proposals.callback_inbox import repository as repo_module
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 795_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-{update_id}"
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    barrier = asyncio.Barrier(2)
    original_insert = repo_module.CallbackInboxRepository.insert

    async def _barriered_insert(self, **fields: Any):
        await barrier.wait()
        return await original_insert(self, **fields)

    monkeypatch.setattr(
        repo_module.CallbackInboxRepository, "insert", _barriered_insert, raising=True
    )

    variant = _envelope_variant(None)

    async def _attempt():
        return await ingest_callback_update(
            make_update(
                data=variant["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )

    first, second = await asyncio.gather(_attempt(), _attempt())
    job_ids = {result.job_id for result in (first, second) if result.job_id}
    assert len(job_ids) == 1
    job_id = job_ids.pop()
    inbox_cleanup.append(job_id)

    assert {result.reason for result in (first, second)} == {"queued", "duplicate"}
    assert all(result.accepted for result in (first, second))
    assert kicked == [job_id]


@pytest.mark.asyncio
async def test_persist_failure_raises_and_never_attempts_the_kick(
    _bootstrap_test_schema,
) -> None:
    """RED item 2 — enqueue count is 0 once the commit has failed."""
    import contextlib

    from app.services.order_proposals.callback_inbox.ingress import (
        CallbackInboxUnavailable,
        ingest_callback_update,
    )

    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:  # pragma: no cover - must not run
        kicked.append(job_id)

    class _ExplodingSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("db down")

        def add(self, *args, **kwargs) -> None:
            return None

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
            make_update(data=_valid_callback_data(), update_id=730_001),
            now=now_kst(),
            session_factory=_factory,
            enqueue_fn=_enqueue,
        )
    assert kicked == []


@pytest.mark.asyncio
async def test_a_failure_at_the_real_commit_returns_503_and_never_enqueues(
    _bootstrap_test_schema,
) -> None:
    """R6 strengthening 2 — fail at ``commit()``, not before it.

    The sibling test explodes during ``execute``/``flush``, which never
    reaches the interesting boundary. Here the INSERT really is built and
    flushed against the real session, and only the commit fails -- the shape
    of a lost connection at exactly the wrong moment. The row must not exist,
    the caller must get ``CallbackInboxUnavailable``, and the producer must
    never have been asked to do anything.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        CallbackInboxUnavailable,
        ingest_callback_update,
    )

    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:  # pragma: no cover - must not run
        kicked.append(job_id)

    update_id = 745_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-{update_id}"
    flushed: list[int] = []

    class _CommitFails:
        """A real session whose commit is the only thing that breaks."""

        def __init__(self, session) -> None:
            self._session = session

        def __getattr__(self, name):
            return getattr(self._session, name)

        async def flush(self, *args, **kwargs):
            result = await self._session.flush(*args, **kwargs)
            flushed.append(1)
            return result

        async def commit(self):
            raise ConnectionResetError("connection lost at commit")

    @contextlib.asynccontextmanager
    async def _factory():
        async with AsyncSessionLocal() as session:
            try:
                yield _CommitFails(session)
            finally:
                await session.rollback()

    with pytest.raises(CallbackInboxUnavailable):
        await ingest_callback_update(
            make_update(
                data=_valid_callback_data(),
                update_id=update_id,
                callback_id=callback_id,
            ),
            now=now_kst(),
            session_factory=_factory,
            enqueue_fn=_enqueue,
        )

    assert flushed == [1], "the insert never reached the real session"
    assert kicked == [], "a failed commit still kicked the queue"
    digest = build_update_digest(update_id=update_id, callback_query_id=callback_id)
    assert await _count_rows(digest) == 0, "an uncommitted row became visible"


@pytest.mark.asyncio
async def test_the_ack_is_bounded_by_the_configured_enqueue_timeout(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R9 B20 — the bound is the configured timeout, not "under ten seconds".

    A kick that never completes must cost exactly the configured budget plus
    scheduler overhead. The old assertion allowed nearly 10s for a 0.05s
    setting, which would have passed even if the timeout were ignored
    entirely and the request had waited on something else.
    """
    from app.core.config import settings
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    timeout_seconds = 0.25
    monkeypatch.setattr(
        settings,
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_ENQUEUE_TIMEOUT_SECONDS",
        timeout_seconds,
        raising=False,
    )

    started_waiting = asyncio.Event()
    never = asyncio.Event()

    async def _never_completes(job_id: uuid.UUID) -> None:
        started_waiting.set()
        await never.wait()

    update_id = 742_000 + uuid.uuid4().int % 10_000
    began = time.monotonic()
    result = await ingest_callback_update(
        make_update(
            data=_valid_callback_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_never_completes,
    )
    elapsed = time.monotonic() - began
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    assert started_waiting.is_set(), "the producer was never called"
    assert result.accepted is True
    assert result.enqueued is False
    # It really waited the budget ...
    assert elapsed >= timeout_seconds, elapsed
    # ... and then gave up, rather than waiting on anything else.
    assert elapsed < timeout_seconds + 1.0, (
        f"the ACK took {elapsed:.3f}s for a {timeout_seconds}s enqueue budget"
    )

    row = await load_job(result.job_id)
    assert row is not None
    assert row.state == "pending"


@pytest.mark.asyncio
async def test_a_cancellation_resistant_kick_keeps_the_committed_row_and_returns_bounded(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R33 — a cancellation-resistant Redis producer cannot extend the ACK."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 740_000 + uuid.uuid4().int % 10_000
    started = asyncio.Event()
    cancel_requested = asyncio.Event()
    release = asyncio.Event()
    produced_job_ids: list[uuid.UUID] = []
    producer_started_at = 0.0

    async def _resistant(job_id: uuid.UUID) -> None:
        nonlocal producer_started_at
        produced_job_ids.append(job_id)
        producer_started_at = time.monotonic()
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancel_requested.set()

    request = asyncio.create_task(
        ingest_callback_update(
            make_update(
                data=_valid_callback_data(),
                update_id=update_id,
                callback_id=f"cbq-{update_id}",
            ),
            now=now_kst(),
            enqueue_fn=_resistant,
            enqueue_timeout_seconds=0.02,
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
        # The fake deliberately becomes cooperative only after the ACK has
        # returned.  Let its real done callback own final task cleanup.
        release.set()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(request, timeout=1.0)
        await asyncio.sleep(0)
        for job_id in produced_job_ids:
            if job_id not in inbox_cleanup:
                inbox_cleanup.append(job_id)


@pytest.mark.asyncio
async def test_a_failing_kick_keeps_the_committed_row(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 750_000 + uuid.uuid4().int % 10_000

    async def _boom(job_id: uuid.UUID) -> None:
        raise ConnectionError("redis is gone")

    result = await ingest_callback_update(
        make_update(
            data=_valid_callback_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_boom,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    assert result.accepted is True
    assert result.enqueued is False
    row = await load_job(result.job_id)
    assert row is not None and row.state == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("update", "reason"),
    [
        ({"update_id": 1}, "not_callback"),
        (
            {
                "update_id": 2,
                "callback_query": {
                    "id": "cbq-bad",
                    "from": {"id": 777},
                    "message": {"chat": {"id": CHAT_ID}, "message_id": 5},
                    "data": "not-a-valid-callback",
                },
            },
            "malformed_callback_data",
        ),
        (
            {
                "update_id": 3,
                "callback_query": {
                    "id": "cbq-foreign",
                    "from": {"id": 777},
                    "message": {"chat": {"id": 999999}, "message_id": 5},
                    "data": "op:0123abcd:AAAAAAAAAAAAAAAAAAAAAA:1:abcdefghijkl:n1",
                },
            },
            "chat_not_allowed",
        ),
    ],
)
async def test_rejected_updates_persist_nothing_and_kick_nothing(
    _bootstrap_test_schema, update: dict[str, Any], reason: str
) -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:  # pragma: no cover - must not run
        kicked.append(job_id)

    async with AsyncSessionLocal() as session:
        before = (
            await session.execute(
                select(func.count()).select_from(TelegramCallbackInboxJob)
            )
        ).scalar_one()

    result = await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)
    assert result.accepted is False
    assert result.job_id is None
    assert result.reason == reason
    assert kicked == []

    async with AsyncSessionLocal() as session:
        after = (
            await session.execute(
                select(func.count()).select_from(TelegramCallbackInboxJob)
            )
        ).scalar_one()
    assert after == before


@pytest.mark.asyncio
async def test_an_update_without_any_delivery_identity_is_refused(
    _bootstrap_test_schema,
) -> None:
    """No ``update_id`` and no callback-query id means no dedupe key: fail closed."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    async def _never(job_id: uuid.UUID) -> None:  # pragma: no cover - must not run
        raise AssertionError("kicked an update with no dedupe key")

    result = await ingest_callback_update(
        {
            "callback_query": {
                "from": {"id": 777},
                "message": {"chat": {"id": CHAT_ID}, "message_id": 5},
                "data": _valid_callback_data(),
            }
        },
        now=now_kst(),
        enqueue_fn=_never,
    )
    assert result.accepted is False
    assert result.reason == "no_delivery_identity"


# ---------------------------------------------------------------------------
# R11 — the equality projection itself
# ---------------------------------------------------------------------------

#: The exact eleven fields ingress persists. Equality must cover all of them.
PERSISTED_ENVELOPE_FIELDS = (
    "callback_query_id",
    # R28: a one-way digest of the Telegram update id. The identity is the
    # callback query id alone, so a redelivery under a different update id
    # lands on the same row and is only caught by comparing this.
    "update_identity_digest",
    "chat_id",
    "message_id",
    "telegram_user_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)


@pytest.mark.unit
def test_equality_and_insert_share_one_projection() -> None:
    """R11 — drift between "what we store" and "what we compare" is the bug.

    ``callback_query_id`` cannot be raced through the concurrent test because
    changing it changes the delivery digest, so there is no conflict to have.
    That is exactly why the projection is pinned here directly: it is the one
    field whose coverage the race cannot demonstrate.
    """
    from app.services.order_proposals.callback_inbox.ingress import (
        normalized_envelope_projection,
    )
    from app.services.order_proposals.telegram_callback import (
        normalize_callback_update,
    )

    variant = _envelope_variant(None)
    normalized = normalize_callback_update(
        make_update(
            data=variant["data"],
            update_id=1,
            callback_id="cbq-projection",
            chat_id=variant["chat_id"],
            user_id=variant["user_id"],
            message_id=variant["message_id"],
        )
    )
    projection = normalized_envelope_projection(normalized)
    assert set(projection) == set(PERSISTED_ENVELOPE_FIELDS), sorted(projection)

    # The projection is what a stored row is compared against, field for field.
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    for field in PERSISTED_ENVELOPE_FIELDS:
        assert field in TelegramCallbackInboxJob.__table__.columns, field

    # Normalisation matches the insert exactly: str-or-None, never int.
    assert projection["telegram_user_id"] == str(variant["user_id"])
    assert projection["chat_id"] == str(variant["chat_id"])
    assert projection["callback_query_id"] == "cbq-projection"
    assert projection["message_id"] == variant["message_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", PERSISTED_ENVELOPE_FIELDS)
async def test_matches_rejects_a_row_differing_in_any_single_field(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], field: str
) -> None:
    """R11 — one-field-at-a-time over all eleven, including the digest-bound one."""
    from app.services.order_proposals.callback_inbox.ingress import (
        envelope_matches_row,
    )
    from app.services.order_proposals.telegram_callback import (
        normalize_callback_update,
    )

    baseline = _envelope_variant(None)
    normalized = normalize_callback_update(
        make_update(
            data=baseline["data"],
            update_id=1,
            callback_id="cbq-base",
            chat_id=baseline["chat_id"],
            user_id=baseline["user_id"],
            message_id=baseline["message_id"],
        )
    )

    class _Row:
        pass

    row = _Row()
    from app.services.order_proposals.callback_inbox.ingress import (
        normalized_envelope_projection,
    )

    projection = normalized_envelope_projection(normalized)
    for key, value in projection.items():
        setattr(row, key, value)
    assert envelope_matches_row(row, normalized) is True, "baseline must match"

    # Perturb exactly one field.
    current = getattr(row, field)
    if field == "message_id":
        setattr(row, field, (current or 0) + 1)
    elif field == "membership_revision":
        setattr(row, field, (current or 0) + 1)
    elif field == "dispatch_attempt_id":
        setattr(row, field, uuid.uuid4())
    else:
        setattr(row, field, f"{current}-different")
    assert envelope_matches_row(row, normalized) is False, (
        f"a row differing in {field!r} was treated as the same call"
    )


@pytest.mark.asyncio
async def test_a_redelivery_landing_on_a_terminal_row_is_a_benign_duplicate(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R11 — after the scrub, equality is deliberately no longer decidable.

    A terminal row has all eleven equality fields NULL, so an exact redelivery and
    a tampered one are indistinguishable. Rather than retain a reconstructible
    binding fingerprint to tell them apart -- which would undo the privacy
    contract the scrub exists for -- a terminal digest hit is treated as a
    benign duplicate: acknowledged, never rehydrated, never queued.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 798_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-{update_id}"
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    baseline = _envelope_variant(None)
    first = await ingest_callback_update(
        make_update(
            data=baseline["data"],
            update_id=update_id,
            callback_id=callback_id,
            chat_id=baseline["chat_id"],
            user_id=baseline["user_id"],
            message_id=baseline["message_id"],
        ),
        now=now_kst(),
        enqueue_fn=_enqueue,
    )
    assert first.job_id is not None
    inbox_cleanup.append(first.job_id)
    assert kicked == [first.job_id]

    # Drive it to a real terminal state, which scrubs all eleven fields.
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    async def _handler(normalized, **kwargs):
        return {"handled": False, "reason": "proposal_not_found"}

    assert (await process_callback_job(first.job_id, handler=_handler))[
        "status"
    ] == "discarded"
    row = await load_job(first.job_id)
    assert row is not None and row.state == "discarded"
    assert row.nonce is None and row.chat_id is None

    for variant in (_envelope_variant(None), _envelope_variant("nonce")):
        again = await ingest_callback_update(
            make_update(
                data=variant["data"],
                update_id=update_id,
                callback_id=callback_id,
                chat_id=variant["chat_id"],
                user_id=variant["user_id"],
                message_id=variant["message_id"],
            ),
            now=now_kst(),
            enqueue_fn=_enqueue,
        )
        assert again.accepted is True
        assert again.duplicate is True
        assert again.reason == "duplicate"
        assert again.enqueued is False, "a terminal redelivery was re-queued"

    # Nothing was rehydrated: the row is still terminal and still scrubbed.
    final = await load_job(first.job_id)
    assert final is not None
    assert final.state == "discarded"
    assert final.nonce is None and final.chat_id is None
    assert kicked == [first.job_id]
    digest = build_update_digest(update_id=update_id, callback_query_id=callback_id)
    assert await _count_rows(digest) == 1


@pytest.mark.asyncio
async def test_the_conflict_seam_really_is_two_backends_waiting_on_the_index(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R11 — the loser blocks on PostgreSQL's unique index, not on Python.

    A holds its INSERT open after the real flush; B then flushes the same
    delivery digest and must *not* complete until A commits, because that is
    what a unique index does. The two are on distinct backends, proved by
    ``pg_backend_pid()``.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    digest = uuid.uuid4().hex * 2
    pids: list[int] = []

    def _row_fields(nonce: str) -> dict[str, Any]:
        return {
            "update_digest": digest,
            "now": now_kst(),
            "callback_query_id": "cbq-seam",
            "update_identity_digest": None,
            "chat_id": str(CHAT_ID),
            "message_id": 555,
            "telegram_user_id": "777",
            "action": "op",
            "subject_short": "0123abcd",
            "dispatch_attempt_id": uuid.uuid4(),
            "membership_revision": 1,
            "membership_digest": "abcdefghijkl",
            "nonce": nonce,
        }

    a_flushed = asyncio.Event()
    a_may_commit = asyncio.Event()
    b_finished = asyncio.Event()

    async def _writer_a() -> uuid.UUID:
        async with AsyncSessionLocal() as session:
            pids.append(
                int(
                    (
                        await session.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                )
            )
            row = await CallbackInboxService(session).enqueue(
                **_row_fields("aaaaaaaaaaa")
            )
            job_id = row.job_id
            a_flushed.set()
            await a_may_commit.wait()
            await session.commit()
            return job_id

    async def _writer_b() -> bool:
        async with AsyncSessionLocal() as session:
            pids.append(
                int(
                    (
                        await session.execute(text("SELECT pg_backend_pid()"))
                    ).scalar_one()
                )
            )
            try:
                await CallbackInboxService(session).enqueue(
                    **_row_fields("bbbbbbbbbbb")
                )
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()
                return False
            finally:
                b_finished.set()

    task_a = asyncio.create_task(_writer_a())
    await asyncio.wait_for(a_flushed.wait(), timeout=15)
    task_b = asyncio.create_task(_writer_b())

    # B is now waiting on the index. Give it real time to prove it is stuck.
    await asyncio.sleep(0.3)
    assert not b_finished.is_set(), "B did not block on the unique index"

    a_may_commit.set()
    job_id = await task_a
    inbox_cleanup.append(job_id)
    b_won = await asyncio.wait_for(task_b, timeout=15)

    assert b_won is False, "both writers committed the same delivery digest"
    assert len(set(pids)) == 2, f"the two writers shared a backend: {pids}"

    # The winner is readable from an independent session, and it is A's.
    async with AsyncSessionLocal() as session:
        stored = await CallbackInboxService(session).get_by_update_digest(digest)
        assert stored is not None
        assert stored.job_id == job_id
        assert stored.nonce == "aaaaaaaaaaa"
    assert await _count_rows(digest) == 1
