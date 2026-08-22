"""W5 — ``retry_wait`` may only ever mean "pre-core failure".

Adversarial review R21. ``schedule_retry``'s CAS predicate guards *who* may
earn a retry -- a row that provably never entered the core. It did not guard
*what the retry says it is*: ``error_class`` and ``outcome`` were ordinary
caller-supplied arguments, so any caller could park a job in ``retry_wait``
labelled ``handler_ambiguous``/``internal_error``.

That is not cosmetic. The whole W5 retry algebra is "``retry_wait`` means, and
only means, a failure that provably never reached the mutating region". A row
that says ``retry_wait`` while claiming an ambiguous handler outcome is a
contradiction an operator (or a future recovery rule) could read either way.

Two layers, because a service-level rule alone can be walked around by the
next writer:

* the API takes no retry vocabulary at all -- there is nothing to pass;
* PostgreSQL refuses ``retry_wait`` with any other ``error_class``, and
  refuses a ``pending`` row that carries an outcome or an error class.
"""

from __future__ import annotations

import inspect
import uuid
from datetime import timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import load_job, make_update, shape_owned_callback_inbox_row

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


async def _queue(inbox_cleanup: list[uuid.UUID]) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 640_000 + uuid.uuid4().int % 100_000

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


# ---------------------------------------------------------------------------
# the API surface
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_schedule_retry_accepts_no_retry_vocabulary_from_its_caller() -> None:
    """R21 — there must be nothing to inject.

    Validating a caller-supplied value is weaker than not having one: the
    parameter is the vulnerability, because every future caller has to be
    reviewed for it.
    """
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    parameters = inspect.signature(CallbackInboxService.schedule_retry).parameters
    assert set(parameters) == {"self", "row", "now"}, sorted(parameters)
    for banned in ("error_class", "outcome", "reason", "state"):
        assert banned not in parameters, banned


@pytest.mark.asyncio
async def test_a_granted_retry_always_records_the_pre_core_vocabulary(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The one vocabulary a retry may carry, written by the service itself."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(job_id, state="processing", attempt_count=1, started_at=now_kst())

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        await service.schedule_retry(row, now=now_kst())
        await session.commit()

    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.state == "retry_wait"
    assert fresh.error_class == "pre_core_failure"
    assert fresh.outcome is None
    assert fresh.available_at > fresh.received_at


# ---------------------------------------------------------------------------
# the database, which the next writer cannot talk around
# ---------------------------------------------------------------------------

_INSERT = sa.text(
    """
    INSERT INTO review.telegram_callback_inbox
        (job_id, update_digest, state, attempt_count, max_attempts,
         received_at, available_at, started_at, chat_id, telegram_user_id, action, subject_short,
         dispatch_attempt_id, membership_revision, membership_digest, nonce,
         outcome, error_class)
    VALUES
        (:job_id, :update_digest, :state, 1, 3, now(), now(), :started_at,
         '42', :telegram_user_id, 'op', '0123abcd', :attempt_id, 1, 'abcdefghijkl', 'nonce123456',
         :outcome, :error_class)
    """
)


def _accepts(
    connection: sa.Connection,
    *,
    state: str,
    outcome: str | None,
    error_class: str | None,
) -> bool:
    savepoint = connection.begin_nested()
    try:
        connection.execute(
            _INSERT,
            {
                "job_id": uuid.uuid4(),
                "update_digest": uuid.uuid4().hex * 2,
                "state": state,
                "started_at": None if state != "processing" else now_kst(),
                "telegram_user_id": (
                    "777" if state in {"pending", "processing", "retry_wait"} else None
                ),
                "attempt_id": uuid.uuid4(),
                "outcome": outcome,
                "error_class": error_class,
            },
        )
    except sa.exc.IntegrityError:
        savepoint.rollback()
        return False
    savepoint.rollback()
    return True


@pytest.mark.asyncio
async def test_the_database_refuses_any_other_retry_vocabulary(
    _bootstrap_test_schema,
) -> None:
    """R21 — ``retry_wait`` implies ``pre_core_failure``, in PostgreSQL."""

    def _probe(connection: sa.Connection) -> dict[str, bool]:
        results = {
            "retry_wait__pre_core_failure": _accepts(
                connection,
                state="retry_wait",
                outcome=None,
                error_class="pre_core_failure",
            ),
        }
        for wrong in (
            "attempt_budget_invalid",
            "handler_ambiguous",
            "handler_exception",
            "chat_revoked",
            "envelope_invalid",
            "attempts_exhausted",
            None,
        ):
            results[f"retry_wait__{wrong}"] = _accepts(
                connection, state="retry_wait", outcome=None, error_class=wrong
            )
        # A retry carries no outcome: an outcome is a *verdict*, and a job
        # that never entered the core produced none.
        results["retry_wait__with_outcome"] = _accepts(
            connection,
            state="retry_wait",
            outcome="internal_error",
            error_class="pre_core_failure",
        )
        # A queued row has not failed at anything yet.
        results["pending__clean"] = _accepts(
            connection, state="pending", outcome=None, error_class=None
        )
        results["pending__with_error_class"] = _accepts(
            connection, state="pending", outcome=None, error_class="pre_core_failure"
        )
        results["pending__with_outcome"] = _accepts(
            connection, state="pending", outcome="approved", error_class=None
        )
        # Terminal states keep their own vocabulary, unchanged.
        results["dead_letter__handler_ambiguous"] = _accepts(
            connection,
            state="dead_letter",
            outcome=None,
            error_class="handler_ambiguous",
        )
        return results

    async with AsyncSessionLocal() as session:
        connection = await session.connection()
        observed = await connection.run_sync(_probe)
        await session.rollback()

    assert observed == {
        "retry_wait__pre_core_failure": True,
        "retry_wait__attempt_budget_invalid": False,
        "retry_wait__handler_ambiguous": False,
        "retry_wait__handler_exception": False,
        "retry_wait__chat_revoked": False,
        "retry_wait__envelope_invalid": False,
        "retry_wait__attempts_exhausted": False,
        "retry_wait__None": False,
        "retry_wait__with_outcome": False,
        "pending__clean": True,
        "pending__with_error_class": False,
        "pending__with_outcome": False,
        "dead_letter__handler_ambiguous": False,
    }


@pytest.mark.unit
def test_the_model_declares_the_retry_vocabulary_check() -> None:
    from app.models.telegram_callback_inbox import TelegramCallbackInboxJob

    checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in TelegramCallbackInboxJob.__table__.constraints
        if isinstance(constraint, sa.CheckConstraint)
    }
    name = "ck_telegram_callback_inbox_retry_vocabulary"
    assert name in checks, sorted(checks)
    expression = checks[name]
    assert "retry_wait" in expression
    assert "pre_core_failure" in expression
    assert "pending" in expression


@pytest.mark.asyncio
async def test_a_caller_cannot_launder_an_ambiguous_failure_into_a_retry(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """The reproduction R21 reported, asserted to be impossible.

    Previously this wrote ``state=retry_wait, error_class=handler_ambiguous,
    outcome=internal_error`` and the database accepted it.
    """
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _force(job_id, state="processing", attempt_count=1, started_at=now_kst())

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        with pytest.raises(TypeError):
            await service.schedule_retry(  # type: ignore[call-arg]
                row,
                now=now_kst(),
                error_class="handler_ambiguous",
                outcome="internal_error",
            )
        await session.rollback()

    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.state == "processing", "a refused call still moved the row"
    assert fresh.error_class is None


@pytest.mark.asyncio
async def test_a_retry_row_survives_a_second_retry_with_the_same_vocabulary(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Two consecutive pre-core failures stay inside the one vocabulary."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    for attempt in (1, 2):
        await _force(
            job_id, state="processing", attempt_count=attempt, started_at=now_kst()
        )
        async with AsyncSessionLocal() as session:
            service = CallbackInboxService(session)
            row = await service.get(job_id)
            assert row is not None
            await service.schedule_retry(row, now=now_kst())
            await session.commit()

        fresh = await load_job(job_id)
        assert fresh is not None
        assert fresh.state == "retry_wait"
        assert fresh.error_class == "pre_core_failure"
        assert fresh.outcome is None
        assert fresh.available_at > fresh.received_at + timedelta(seconds=-1)
