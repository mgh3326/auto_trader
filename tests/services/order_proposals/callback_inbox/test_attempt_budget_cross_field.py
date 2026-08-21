"""W5 R34 — fixed attempt-budget cross-field invariant and poison scrub.

The database is authoritative, but a binary predating the fixed ``3`` budget
could have persisted a row that today's worker must never execute.  These
tests construct that legacy shape only through the run-owned DDL bypass in
``conftest`` and then drive the production worker/recovery paths.
"""

from __future__ import annotations

import contextlib
import inspect
import logging
import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst

from .conftest import (
    attempt_budget_poison_rows,
    load_job,
    make_update,
)

pytestmark = pytest.mark.integration


AUTHORITY_FIELDS: tuple[str, ...] = (
    "callback_query_id",
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

    update_id = 740_000 + uuid.uuid4().int % 100_000

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


_TEST_ROW_FIELDS: frozenset[str] = frozenset(
    {
        "state",
        "attempt_count",
        "max_attempts",
        "available_at",
        "started_at",
        "handler_entered_at",
        "handler_completed_at",
        "terminal_state_pending",
        "outcome",
        "error_class",
    }
)


async def _set_row(job_id: uuid.UUID, **fields: Any) -> None:
    """Test-side raw update; R34 never imports a shipped test mutator."""
    unknown = set(fields) - _TEST_ROW_FIELDS
    assert not unknown, sorted(unknown)
    assert fields
    assignments = ", ".join(f"{field} = :{field}" for field in sorted(fields))
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            sa.text(
                "UPDATE review.telegram_callback_inbox "
                f"SET {assignments} WHERE job_id = :job_id"
            ),
            {"job_id": job_id, **fields},
        )
        assert result.rowcount == 1
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


def _assert_budget_scrubbed(raw: dict[str, Any], *, expected_attempt: int) -> None:
    assert raw["state"] == "dead_letter"
    assert raw["error_class"] == "attempt_budget_invalid"
    assert raw["max_attempts"] == 3
    assert raw["attempt_count"] == expected_attempt
    assert raw["finished_at"] is not None
    for field in AUTHORITY_FIELDS:
        assert raw[field] is None, field


@pytest.mark.unit
@pytest.mark.parametrize(
    ("attempt_count", "max_attempts", "expected_malformed"),
    (
        (0, 3, False),
        (3, 3, False),
        (3, 4, True),
        (0, 1, True),
        (0, 0, True),
        (0, -1, True),
        (-1, 3, True),
        (4, 3, True),
        (2, 1, True),
        (None, 3, True),
        (0, None, True),
        ("3", 3, True),
        (True, 3, True),
        (0, True, True),
    ),
)
def test_the_malformed_budget_validator_is_pure_and_fixed(
    attempt_count: object, max_attempts: object, expected_malformed: bool
) -> None:
    from app.services.order_proposals.callback_inbox.contracts import (
        is_malformed_attempt_budget,
    )

    assert (
        is_malformed_attempt_budget(
            attempt_count=attempt_count, max_attempts=max_attempts
        )
        is expected_malformed
    )


@pytest.mark.unit
def test_claim_classification_checks_malformed_budget_before_any_active_path() -> None:
    """R34 — malformed wins before repair, exhaustion, or due-time logic."""
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    now = now_kst()
    service = CallbackInboxService(None)  # type: ignore[arg-type]

    def _row(**overrides: Any) -> SimpleNamespace:
        fields: dict[str, Any] = {
            "state": "pending",
            "attempt_count": 0,
            "max_attempts": 3,
            "available_at": now,
            "started_at": None,
            "handler_entered_at": None,
            "handler_completed_at": None,
            "terminal_state_pending": None,
        }
        fields.update(overrides)
        return SimpleNamespace(**fields)

    cases = {
        "malformed_pending": (
            _row(attempt_count=3, max_attempts=4),
            ("malformed", None),
        ),
        "repair_shaped_processing": (
            _row(
                state="processing",
                started_at=now,
                handler_entered_at=now,
                handler_completed_at=now,
                terminal_state_pending="succeeded",
            ),
            ("repair", None),
        ),
        "malformed_repair_shaped_processing": (
            _row(
                state="processing",
                attempt_count=-1,
                started_at=now,
                handler_entered_at=now,
                handler_completed_at=now,
                terminal_state_pending="succeeded",
            ),
            ("malformed", None),
        ),
        "entered_only_processing": (
            _row(
                state="processing",
                started_at=now,
                handler_entered_at=now,
            ),
            ("ambiguous", None),
        ),
        "malformed_entered_only_processing": (
            _row(
                state="processing",
                attempt_count=-1,
                started_at=now,
                handler_entered_at=now,
            ),
            ("malformed", None),
        ),
        "future_malformed_retry_not_due": (
            _row(
                state="retry_wait",
                attempt_count=3,
                max_attempts=4,
                available_at=now + timedelta(hours=6),
            ),
            ("malformed", None),
        ),
        "future_malformed_retry_exhausted": (
            _row(
                state="retry_wait",
                attempt_count=4,
                max_attempts=3,
                available_at=now + timedelta(hours=6),
            ),
            ("malformed", None),
        ),
        "canonical_spent_retry": (
            _row(
                state="retry_wait",
                attempt_count=3,
                max_attempts=3,
                available_at=now + timedelta(hours=6),
            ),
            ("exhausted", None),
        ),
        "healthy_future_retry": (
            _row(
                state="retry_wait",
                attempt_count=2,
                max_attempts=3,
                available_at=now + timedelta(hours=6),
            ),
            ("skip", "not_due"),
        ),
        "valid_pending": (_row(), ("run", None)),
    }

    observed = {
        name: (
            decision.action,
            decision.reason,
        )
        for name, (row, _expected) in cases.items()
        for decision in (
            service.classify_claim(
                row, now=now, claimable_states=RECOVERY_CLAIMABLE_STATES
            ),
        )
    }
    assert observed == {name: expected for name, (_row, expected) in cases.items()}


@pytest.mark.asyncio
async def test_enqueue_has_no_budget_override_and_persists_fixed_three(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ingress owns no tunable attempt budget surface."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    assert (
        "max_attempts" not in inspect.signature(CallbackInboxService.enqueue).parameters
    )
    # These are deliberately not Settings fields. A direct environment read in
    # an ingress/service path would make the persisted row prove it wrong.
    for name in (
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_MAX_ATTEMPTS",
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_RETRY_LIMIT",
        "ORDER_PROPOSALS_TELEGRAM_CALLBACK_ATTEMPT_BUDGET",
    ):
        monkeypatch.setenv(name, "99")
    job_id = await _queue(inbox_cleanup)
    raw = await _raw_row(job_id)
    assert raw["attempt_count"] == 0
    assert raw["max_attempts"] == 3


@pytest.mark.asyncio
async def test_worker_scrubs_a_malformed_budget_before_handler_entry_and_telemetry(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """R34 — direct worker path has no raw budget value to report."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.worker import process_callback_job

    job_id = await _queue(inbox_cleanup)
    handler_calls: list[int] = []
    span_data: dict[str, Any] = {}

    class _Span:
        def set_data(self, key: str, value: Any) -> None:
            span_data[key] = value

    @contextlib.contextmanager
    def _transaction(_job_id: uuid.UUID):
        yield _Span()

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        handler_calls.append(1)
        return {"handled": True, "reason": "approved"}

    monkeypatch.setattr(worker_module, "worker_transaction", _transaction)
    async with attempt_budget_poison_rows() as poison:
        await poison.insert(
            job_id,
            state="pending",
            attempt_count=4,
            max_attempts=3,
            available_at=now_kst(),
        )
        with caplog.at_level(logging.INFO):
            result = await poison.run_after_enforcing(
                lambda: process_callback_job(job_id, handler=_handler)
            )
        raw = await _raw_row(job_id)

    assert result == {"status": "dead_letter", "job_id": str(job_id)}
    assert handler_calls == []
    _assert_budget_scrubbed(raw, expected_attempt=3)

    # The TaskIQ result is status + opaque id only.  The terminal telemetry
    # reports the normalised safe count, never the malformed incoming budget.
    assert set(result) == {"status", "job_id"}
    assert span_data["callback_job.attempt"] == 3
    assert "callback_job.max_attempts" not in span_data
    assert 4 not in span_data.values()
    events = [
        record
        for record in caplog.records
        if record.getMessage()
        == "order_proposals.telegram.callback_job_attempt_budget_invalid"
    ]
    assert len(events) == 1
    assert events[0].__dict__["callback_job.attempt"] == 3
    assert "callback_job.max_attempts" not in events[0].__dict__
    event_budget_data = {
        key: value
        for key, value in events[0].__dict__.items()
        if key.startswith("callback_job.")
    }
    assert 4 not in event_budget_data.values()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "attempt_count", "max_attempts", "extra", "normalised_attempt"),
    (
        (
            "pending",
            3,
            4,
            {"available_at": now_kst()},
            3,
        ),
        (
            "processing",
            -2,
            3,
            {
                "started_at": now_kst(),
                "handler_entered_at": now_kst(),
                "handler_completed_at": now_kst(),
                "terminal_state_pending": "succeeded",
                "outcome": "approved",
            },
            0,
        ),
        (
            "retry_wait",
            4,
            3,
            {
                "available_at": now_kst() + timedelta(hours=6),
                "error_class": "pre_core_failure",
            },
            3,
        ),
    ),
)
async def test_recovery_scrubs_every_active_malformed_budget_in_one_sweep(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    state: str,
    attempt_count: int,
    max_attempts: int,
    extra: dict[str, Any],
    normalised_attempt: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Pending, repair-shaped processing, and future retry are priority scrubbed."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    job_id = await _queue(inbox_cleanup)
    handler_calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        handler_calls.append(1)
        return {"handled": True, "reason": "approved"}

    async with attempt_budget_poison_rows() as poison:
        await poison.insert(
            job_id,
            state=state,
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            **extra,
        )
        with caplog.at_level(logging.ERROR):
            first = await poison.run_after_enforcing(
                lambda: recover_callback_jobs(handler=_handler, limit=20)
            )
        after_first = await _raw_row(job_id)
        second = await poison.run_after_enforcing(
            lambda: recover_callback_jobs(handler=_handler, limit=20)
        )
        after_second = await _raw_row(job_id)

    assert first["status"] == "ok"
    assert first["statuses"].get("error", 0) == 0
    assert second["statuses"].get("error", 0) == 0
    assert not any(
        record.getMessage() == "order_proposals.telegram.callback_recovery_job_failed"
        for record in caplog.records
    )
    _assert_budget_scrubbed(after_first, expected_attempt=normalised_attempt)
    assert handler_calls == []
    # The target was terminal after the first sweep; a second sweep neither
    # re-enters it nor alters its finished terminal record.
    assert second["status"] == "ok"
    assert after_second == after_first


@pytest.mark.asyncio
async def test_malformed_future_retry_precedes_the_normal_exhausted_tier(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R34 — malformed means scrub now, ahead of ordinary exhausted cleanup."""
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )

    malformed = await _queue(inbox_cleanup)
    exhausted = await _queue(inbox_cleanup)
    calls: list[int] = []

    async def _handler(normalized, **kwargs):  # pragma: no cover - must not run
        calls.append(1)
        return {"handled": True, "reason": "approved"}

    async with attempt_budget_poison_rows() as poison:
        future = now_kst() + timedelta(hours=6)
        await poison.insert(
            malformed,
            state="retry_wait",
            attempt_count=3,
            max_attempts=4,
            error_class="pre_core_failure",
            available_at=future,
        )
        await poison.insert(
            exhausted,
            state="retry_wait",
            attempt_count=3,
            max_attempts=3,
            error_class="pre_core_failure",
            available_at=future,
        )
        report = await poison.run_after_enforcing(
            lambda: recover_callback_jobs(handler=_handler, limit=1)
        )
        malformed_raw = await _raw_row(malformed)
        exhausted_raw = await _raw_row(exhausted)

    assert report["claimed"] == 1
    _assert_budget_scrubbed(malformed_raw, expected_attempt=3)
    assert exhausted_raw["state"] == "retry_wait"
    assert exhausted_raw["error_class"] == "pre_core_failure"
    assert calls == []


@pytest.mark.asyncio
async def test_malformed_priority_preserves_queued_and_stale_recovery_progress(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R34 keeps R29 fairness: one poison cannot consume a whole tick."""
    from app.services.order_proposals.callback_inbox.contracts import (
        recovery_scan_cap,
    )
    from app.services.order_proposals.callback_inbox.recovery import (
        recover_callback_jobs,
    )
    from app.services.order_proposals.callback_inbox.repository import (
        CallbackInboxRepository,
    )

    poison_ids = [await _queue(inbox_cleanup) for _ in range(3)]
    queued = await _queue(inbox_cleanup)
    stale = await _queue(inbox_cleanup)
    await _set_row(
        stale,
        state="processing",
        attempt_count=1,
        started_at=now_kst() - timedelta(hours=6),
    )
    handler_calls: list[str] = []

    async def _handler(normalized, **kwargs):
        handler_calls.append(normalized.callback.subject_short)
        return {"handled": True, "reason": "approved"}

    async def _candidate_rows():
        async with AsyncSessionLocal() as session:
            candidates = await CallbackInboxRepository(session).claimable_job_ids(
                now=now_kst(),
                stale_before=now_kst() - timedelta(seconds=1),
                limit=3,
            )
            await session.rollback()
            return candidates

    async with attempt_budget_poison_rows() as poison:
        for job_id in poison_ids:
            await poison.insert(
                job_id,
                state="pending",
                attempt_count=3,
                max_attempts=4,
                available_at=now_kst(),
            )

        candidates = await poison.run_after_enforcing(_candidate_rows)
        candidate_ids = [job_id for job_id, _tier in candidates]
        report = await poison.run_after_enforcing(
            lambda: recover_callback_jobs(handler=_handler, limit=3)
        )
        poison_rows = [await _raw_row(job_id) for job_id in poison_ids]
        queued_row = await _raw_row(queued)
        stale_row = await _raw_row(stale)

    assert len(candidate_ids) == len(set(candidate_ids))
    assert len(candidate_ids) <= recovery_scan_cap(3)
    assert report["claimed"] == 3
    assert sum(row["state"] == "dead_letter" for row in poison_rows) == 1
    assert queued_row["state"] == "succeeded"
    assert stale_row["state"] == "succeeded"
    assert len(handler_calls) == len(set(handler_calls)) == 2


@pytest.mark.asyncio
async def test_malformed_rows_are_excluded_from_the_normal_recovery_tiers(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Every malformed active shape has one, due-independent scan tier only."""
    from app.services.order_proposals.callback_inbox.contracts import TIER_MALFORMED
    from app.services.order_proposals.callback_inbox.repository import (
        CallbackInboxRepository,
    )

    pending = await _queue(inbox_cleanup)
    processing = await _queue(inbox_cleanup)
    future_retry = await _queue(inbox_cleanup)

    async def _candidate_rows():
        async with AsyncSessionLocal() as session:
            candidates = await CallbackInboxRepository(session).claimable_job_ids(
                now=now_kst(),
                stale_before=now_kst() - timedelta(seconds=1),
                limit=20,
            )
            await session.rollback()
            return candidates

    async with attempt_budget_poison_rows() as poison:
        await poison.insert(
            pending,
            state="pending",
            attempt_count=3,
            max_attempts=4,
            available_at=now_kst(),
        )
        await poison.insert(
            processing,
            state="processing",
            attempt_count=-1,
            max_attempts=3,
            started_at=now_kst(),
        )
        await poison.insert(
            future_retry,
            state="retry_wait",
            attempt_count=3,
            max_attempts=4,
            error_class="pre_core_failure",
            available_at=now_kst() + timedelta(hours=6),
        )
        candidates = await poison.run_after_enforcing(_candidate_rows)

    tiers = dict(candidates)
    assert {pending, processing, future_retry}.issubset(tiers)
    assert {tiers[pending], tiers[processing], tiers[future_retry]} == {TIER_MALFORMED}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt_count", "max_attempts"),
    ((3, 4), (2, 1), (0, 1), (-1, 3)),
)
async def test_begin_attempt_cas_refuses_every_malformed_budget_without_writing(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    attempt_count: int,
    max_attempts: int,
) -> None:
    """R34 — a stale caller gets ``False``, never an integrity failure."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)

    async def _begin_attempt() -> bool:
        async with AsyncSessionLocal() as session:
            service = CallbackInboxService(session)
            row = await service.get(job_id)
            assert row is not None
            granted = await service.begin_attempt(row, now=now_kst())
            await session.commit()
            return granted

    async with attempt_budget_poison_rows() as poison:
        await poison.insert(
            job_id,
            state="pending",
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            available_at=now_kst(),
        )
        assert await poison.run_after_enforcing(_begin_attempt) is False
        raw = await _raw_row(job_id)

    assert raw["state"] == "pending"
    assert raw["attempt_count"] == attempt_count
    assert raw["max_attempts"] == max_attempts


@pytest.mark.asyncio
async def test_begin_attempt_cas_refuses_the_canonical_spent_budget_without_writing(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID]
) -> None:
    """A fixed-three row at count three is valid but cannot spend a fourth."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _set_row(job_id, state="pending", attempt_count=3)

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        assert await service.begin_attempt(row, now=now_kst()) is False
        await session.commit()

    raw = await _raw_row(job_id)
    assert raw["state"] == "pending"
    assert raw["attempt_count"] == 3
    assert raw["max_attempts"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_count", (0, 2))
async def test_begin_attempt_cas_still_accepts_canonical_unspent_budgets(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], attempt_count: int
) -> None:
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _set_row(job_id, state="pending", attempt_count=attempt_count)

    async with AsyncSessionLocal() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        assert row is not None
        assert await service.begin_attempt(row, now=now_kst()) is True
        await session.commit()

    fresh = await load_job(job_id)
    assert fresh is not None
    assert fresh.state == "processing"
    assert fresh.max_attempts == 3
    assert fresh.attempt_count == attempt_count + 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("attempt_count", "max_attempts"),
    ((3, 4), (2, 1), (0, 1), (-1, 3)),
)
async def test_schedule_retry_cas_refuses_every_malformed_budget_without_writing(
    _bootstrap_test_schema,
    inbox_cleanup: list[uuid.UUID],
    attempt_count: int,
    max_attempts: int,
) -> None:
    """R34 — malformed rows cannot get a future authoritative retry lease."""
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
        RetryAuthorityRefused,
    )

    job_id = await _queue(inbox_cleanup)

    async def _schedule_retry() -> None:
        async with AsyncSessionLocal() as session:
            service = CallbackInboxService(session)
            row = await service.get(job_id)
            assert row is not None
            with pytest.raises(RetryAuthorityRefused):
                await service.schedule_retry(row, now=now_kst())
            await session.rollback()

    async with attempt_budget_poison_rows() as poison:
        await poison.insert(
            job_id,
            state="processing",
            attempt_count=attempt_count,
            max_attempts=max_attempts,
            started_at=now_kst(),
        )
        await poison.run_after_enforcing(_schedule_retry)
        raw = await _raw_row(job_id)

    assert raw["state"] == "processing"
    assert raw["attempt_count"] == attempt_count
    assert raw["max_attempts"] == max_attempts


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_count", (1, 2))
async def test_schedule_retry_cas_still_accepts_canonical_unspent_budgets(
    _bootstrap_test_schema, inbox_cleanup: list[uuid.UUID], attempt_count: int
) -> None:
    from app.services.order_proposals.callback_inbox.service import (
        CallbackInboxService,
    )

    job_id = await _queue(inbox_cleanup)
    await _set_row(
        job_id,
        state="processing",
        attempt_count=attempt_count,
        started_at=now_kst(),
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
    assert fresh.max_attempts == 3
    assert fresh.attempt_count == attempt_count
    assert fresh.error_class == "pre_core_failure"
