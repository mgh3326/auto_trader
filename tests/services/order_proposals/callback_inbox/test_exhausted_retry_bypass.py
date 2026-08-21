"""W5 — an exhausted job cannot be parked, and cannot be stranded.

Adversarial review R25. R22 closed the worker path: the third pre-core failure
terminates instead of parking. Two ways around it remained.

``schedule_retry`` never checked the budget
    Its CAS asks whether the row is provably pre-entry. It never asked whether
    any attempts were left, so a direct call on an ``attempt_count == max``
    row parked it in ``retry_wait`` with a future ``available_at``.

the recovery scan could not see the result
    ``claimable_job_ids`` gates *every* ``retry_wait`` row on
    ``available_at <= now``, so the row above was invisible to the sweep until
    its backoff elapsed -- and it kept the chat id, the user id and a live
    nonce the whole time.

The existing exhaustion test called ``classify_claim`` directly, which is why
neither showed up: the classifier was right, and nothing ever handed it the
row.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import (
    RETRY_BUDGET_CONSTRAINT,
    make_update,
    shape_owned_callback_inbox_row,
    without_the_retry_budget_check,
)

pytestmark = pytest.mark.integration

AUTHORITY_FIELDS = (
    "callback_query_id",
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

_CONSTRAINT = RETRY_BUDGET_CONSTRAINT


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


async def _queue(inbox_cleanup: list[uuid.UUID]) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 680_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=_synthetic_data(),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


async def _force(job_id: uuid.UUID, **fields: Any) -> None:
    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(session, job_id, **fields)
        await session.commit()


async def _raw_row(job_id: uuid.UUID) -> dict[str, Any]:
    async with AsyncSessionLocal() as session:
        return dict(
            (
                await session.execute(
                    sa.text(
                        "SELECT * FROM review.telegram_callback_inbox "
                        "WHERE job_id = :job_id"
                    ),
                    {"job_id": job_id},
                )
            )
            .mappings()
            .one()
        )


# ---------------------------------------------------------------------------
# 1. the service refuses to park an exhausted job
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schedule_retry_refuses_a_job_with_no_attempts_left(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R25 — the CAS must ask about the budget, not only about the markers."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
        RetryAuthorityRefused,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(job_id, state="processing", attempt_count=3, started_at=now_kst())

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        assert row.attempt_count == row.max_attempts
        with pytest.raises(RetryAuthorityRefused):
            await service.schedule_retry(row, now=now_kst())
        await session.rollback()

    raw = await _raw_row(job_id)
    assert raw["state"] == "processing", "an exhausted job was parked"
    assert raw["attempt_count"] == 3


@pytest.mark.asyncio
async def test_schedule_retry_still_grants_a_job_with_attempts_left(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Anti-vacuity: the budget check must not refuse everything."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(job_id, state="processing", attempt_count=2, started_at=now_kst())

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        await service.schedule_retry(row, now=now_kst())
        await session.commit()

    raw = await _raw_row(job_id)
    assert raw["state"] == "retry_wait"
    assert raw["error_class"] == "pre_core_failure"


# ---------------------------------------------------------------------------
# 2. the database refuses the shape outright
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_model_declares_the_retry_budget_check() -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in TelegramCallbackInboxJob.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    assert _CONSTRAINT in checks, sorted(checks)
    expression = checks[_CONSTRAINT]
    assert "retry_wait" in expression
    assert "attempt_count" in expression
    assert "max_attempts" in expression


_INSERT = sa.text(
    """
    INSERT INTO review.telegram_callback_inbox
        (job_id, update_digest, state, attempt_count, max_attempts,
         received_at, available_at, chat_id, action, subject_short,
         dispatch_attempt_id, membership_revision, membership_digest, nonce,
         error_class)
    VALUES
        (:job_id, :update_digest, :state, :attempt_count, :max_attempts,
         now(), now(), '42', 'op', '0123abcd', :attempt_id, 1,
         'abcdefghijkl', 'nonce123456', :error_class)
    """
)


@pytest.mark.asyncio
async def test_the_database_refuses_an_exhausted_retry_wait_row(
    _bootstrap_test_schema,
) -> None:
    """R25 — no NULL/UNKNOWN loophole: both operands are NOT NULL columns."""

    def _accepts(
        connection: sa.Connection, *, state: str, attempts: int, maximum: int
    ) -> bool:
        savepoint = connection.begin_nested()
        try:
            connection.execute(
                _INSERT,
                {
                    "job_id": uuid.uuid4(),
                    "update_digest": uuid.uuid4().hex * 2,
                    "state": state,
                    "attempt_count": attempts,
                    "max_attempts": maximum,
                    "attempt_id": uuid.uuid4(),
                    "error_class": (
                        "pre_core_failure" if state == "retry_wait" else None
                    ),
                },
            )
        except sa.exc.IntegrityError:
            savepoint.rollback()
            return False
        savepoint.rollback()
        return True

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        return {
            "retry_wait_with_budget": _accepts(
                connection, state="retry_wait", attempts=2, maximum=3
            ),
            "retry_wait_at_budget": _accepts(
                connection, state="retry_wait", attempts=3, maximum=3
            ),
            "retry_wait_over_budget": _accepts(
                connection, state="retry_wait", attempts=3, maximum=1
            ),
            # Other states are unaffected: a spent budget is normal there.
            "processing_at_budget": _accepts(
                connection, state="pending", attempts=3, maximum=3
            ),
        }

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    assert observed == {
        "retry_wait_with_budget": True,
        "retry_wait_at_budget": False,
        "retry_wait_over_budget": False,
        "processing_at_budget": True,
    }


# ---------------------------------------------------------------------------
# 3. the real scanner reaches a legacy exhausted row regardless of its due time
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_real_scanner_terminalises_a_legacy_exhausted_retry_row(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R25 — through ``recover_callback_jobs``, not through the classifier.

    The old test asked the classifier directly and got the right answer, while
    the production scan never selected the row. This drives the real sweep.
    """
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue(inbox_cleanup)
    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    async with without_the_retry_budget_check():
        await _force(
            job_id,
            state="retry_wait",
            attempt_count=3,
            error_class="pre_core_failure",
            # Deliberately far in the future: the sweep must not need to wait.
            available_at=now_kst() + timedelta(hours=6),
        )

        report = await recover_callback_jobs(handler=_handler)
        assert report["status"] == "ok"

        raw = await _raw_row(job_id)

    assert raw["state"] == "dead_letter", raw["state"]
    assert raw["error_class"] == "attempts_exhausted", raw["error_class"]
    assert calls == [], "an exhausted job was handed to the core"
    for field in AUTHORITY_FIELDS:
        assert raw[field] is None, field


@pytest.mark.asyncio
async def test_the_production_recovery_task_reaches_it_too(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], monkeypatch
) -> None:
    """R25 — and through the real TaskIQ entrypoint, both gates armed."""
    from app.core.config import settings
    from app.tasks import telegram_callback_inbox_tasks as task_module

    for gate in (
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED",
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED",
    ):
        monkeypatch.setattr(settings, gate, True, raising=False)

    job_id = await _queue(inbox_cleanup)

    async with without_the_retry_budget_check():
        await _force(
            job_id,
            state="retry_wait",
            attempt_count=3,
            error_class="pre_core_failure",
            available_at=now_kst() + timedelta(hours=6),
        )
        report = await task_module.recover_telegram_callback_jobs()
        assert report["status"] == "ok", report
        raw = await _raw_row(job_id)

    assert raw["state"] == "dead_letter"
    assert raw["error_class"] == "attempts_exhausted"
    for field in AUTHORITY_FIELDS:
        assert raw[field] is None, field


@pytest.mark.asyncio
async def test_a_healthy_not_yet_due_retry_is_still_left_alone(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The backoff still means something for a job that has attempts left."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(
        job_id,
        state="retry_wait",
        attempt_count=1,
        error_class="pre_core_failure",
        available_at=now_kst() + timedelta(hours=6),
    )

    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True}

    await recover_callback_jobs(handler=_handler)

    raw = await _raw_row(job_id)
    assert raw["state"] == "retry_wait", "a due backoff was ignored"
    assert raw["attempt_count"] == 1
    assert calls == []
    assert raw["nonce"] is not None
