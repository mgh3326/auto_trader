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
import time
import uuid
from typing import Any

import pytest
from sqlalchemy import func, select

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


@pytest.mark.asyncio
async def test_a_concurrent_conflicting_delivery_loses_and_fails_closed(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R3 B2 — two *different* calls racing on one delivery identity.

    The sequential mismatch test cannot distinguish a correct implementation
    from one that pre-reads, sees nothing, inserts, and then converts every
    ``IntegrityError`` into "duplicate, already queued". Under a real race the
    loser reaches the unique violation with a genuinely different envelope, and
    reporting that as a benign duplicate would silently drop an approval click
    while telling Telegram everything is fine.

    Whatever the interleaving: exactly one envelope is accepted, exactly one is
    refused as a conflict, exactly one row exists, and exactly one kick fires.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        build_update_digest,
    )
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 780_000 + uuid.uuid4().int % 10_000
    callback_id = f"cbq-{update_id}"
    barrier = asyncio.Barrier(2)
    kicked: list[uuid.UUID] = []

    async def _enqueue(job_id: uuid.UUID) -> None:
        kicked.append(job_id)

    async def _attempt(nonce: str):
        update = make_update(
            data=_valid_callback_data(nonce=nonce),
            update_id=update_id,
            callback_id=callback_id,
        )
        await barrier.wait()
        return await ingest_callback_update(update, now=now_kst(), enqueue_fn=_enqueue)

    first, second = await asyncio.gather(
        _attempt("noncealpha1"), _attempt("noncebravo1")
    )

    accepted = [result for result in (first, second) if result.accepted]
    conflicted = [
        result for result in (first, second) if result.reason == "delivery_conflict"
    ]
    assert len(accepted) == 1, (first, second)
    assert len(conflicted) == 1, (first, second)

    winner = accepted[0]
    assert winner.job_id is not None
    inbox_cleanup.append(winner.job_id)
    assert winner.duplicate is False
    assert winner.reason == "queued"

    loser = conflicted[0]
    assert loser.accepted is False
    assert loser.duplicate is False, "a different envelope is not a duplicate"
    assert loser.job_id is None, "a refused delivery must not name the winner's job"
    assert loser.enqueued is False

    assert kicked == [winner.job_id], kicked
    digest = build_update_digest(update_id=update_id, callback_query_id=callback_id)
    assert await _count_rows(digest) == 1

    # Exactly one authority row, and it is the winner's envelope untouched.
    row = await load_job(winner.job_id)
    assert row is not None
    assert row.nonce in {"noncealpha1", "noncebravo1"}
    assert row.state == "pending"


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
async def test_a_hung_kick_keeps_the_committed_row_and_returns_bounded(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 3 — Redis loss must not undo the durable ACK."""
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 740_000 + uuid.uuid4().int % 10_000

    async def _hang(job_id: uuid.UUID) -> None:
        await asyncio.sleep(30)

    started = time.monotonic()
    result = await ingest_callback_update(
        make_update(
            data=_valid_callback_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_hang,
        enqueue_timeout_seconds=0.05,
    )
    elapsed = time.monotonic() - started
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)

    assert result.accepted is True
    assert result.enqueued is False
    assert elapsed < 5.0

    row = await load_job(result.job_id)
    assert row is not None
    assert row.state == "pending"


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
