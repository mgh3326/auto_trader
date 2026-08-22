"""W5 — the worker's outcome algebra, driven through the *real* callback core.

RED-before-fix items 11, 12, 13 and 14, plus adversarial review R2's retry
algebra and terminal-scrub repair.

Every test in this file runs ``handle_normalized_callback`` for real against a
real, published, committed proposal. The only fake is the broker leg
(``revalidate_fn``), and it is an **exact counter**: the assertion that matters
is always "how many times did a submission function get called", never "did
the code look like it avoided one".
"""

from __future__ import annotations

import functools
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals import OrderProposalsService
from app.services.order_proposals.callback_inbox.contracts import (
    RECOVERY_CLAIMABLE_STATES,
    TERMINAL_STATES,
)
from app.services.order_proposals.revalidation import RungOutcome
from app.services.order_proposals.telegram_callback import (
    handle_normalized_callback,
)

from .conftest import (
    FakeNotifier,
    degrade_owned_callback_subject_short,
    load_job,
    make_update,
    proposal_callback_data,
    seed_proposal,
    shape_owned_callback_inbox_row,
)

pytestmark = pytest.mark.integration


class _BrokerCounter:
    """Counts every simulated broker submission. Never sends anything."""

    def __init__(self, outcome: str = "submitted_acked") -> None:
        self.calls: list[uuid.UUID] = []
        self._outcome = outcome

    async def __call__(self, *, service, proposal_id, now, **kwargs):
        self.calls.append(proposal_id)
        return [RungOutcome(0, self._outcome, {})]

    @property
    def mutations(self) -> int:
        return len(self.calls)


class _SubjectShortSubclass(str):
    """Looks string-like but is not the exact persisted ``str`` type."""


_SUBJECT_DEGRADER_ALLOWED_STATES = ("pending", "retry_wait", "processing")


async def _queue(inbox_cleanup: list[uuid.UUID], group) -> uuid.UUID:
    from app.services.order_proposals.callback_inbox.ingress import (
        ingest_callback_update,
    )

    update_id = 900_000 + uuid.uuid4().int % 100_000

    async def _no_kick(job_id: uuid.UUID) -> None:
        return None

    result = await ingest_callback_update(
        make_update(
            data=proposal_callback_data(group),
            update_id=update_id,
            callback_id=f"cbq-{update_id}",
        ),
        now=now_kst(),
        enqueue_fn=_no_kick,
    )
    assert result.job_id is not None
    inbox_cleanup.append(result.job_id)
    return result.job_id


def _core(broker: _BrokerCounter, notifier: FakeNotifier):
    return functools.partial(
        handle_normalized_callback,
        revalidate_fn=broker,
        notifier=notifier,
    )


async def _reload_group(proposal_id: uuid.UUID):
    async with AsyncSessionLocal() as session:
        group, rungs = await OrderProposalsService(session).get_proposal(proposal_id)
        session.expunge_all()
        return group, rungs


@pytest.mark.asyncio
async def test_the_worker_default_clock_is_now_kst() -> None:
    """R2 — processing time, not receive time, drives every window."""
    from app.services.order_proposals.callback_inbox import worker as worker_module

    assert worker_module.DEFAULT_CLOCK is now_kst


@pytest.mark.asyncio
async def test_a_queued_callback_that_expires_before_processing_never_submits(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 11 — ``received_at < valid_until < processing_at`` is expired.

    Queue delay must never extend ``valid_until``. The exact broker counter is
    the proof: an expired approval submits zero times, and the single-use
    nonce is still unconsumed afterwards, so nothing was spent either.
    """
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="expire12345", symbol="EXPKR")
    job_id = await _queue(inbox_cleanup, group)

    broker = _BrokerCounter()
    notifier = FakeNotifier()
    after_expiry = group.valid_until + timedelta(minutes=5)

    result = await process_callback_job(
        job_id,
        handler=_core(broker, notifier),
        now_fn=lambda: after_expiry,
    )

    assert broker.mutations == 0
    assert result["status"] == "discarded"

    refreshed, _ = await _reload_group(group.proposal_id)
    assert refreshed.approval_nonce_used_at is None, "an expired click spent a nonce"

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "discarded"
    assert row.outcome == "expired"


@pytest.mark.asyncio
async def test_a_replayed_callback_stops_at_the_published_binding_preflight(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 12 — the second delivery reaches no provider and no broker."""
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="replay12345", symbol="RPLKR")
    first_job = await _queue(inbox_cleanup, group)
    second_job = await _queue(inbox_cleanup, group)

    broker = _BrokerCounter()
    notifier = FakeNotifier()

    first = await process_callback_job(first_job, handler=_core(broker, notifier))
    assert first["status"] == "succeeded"
    assert broker.mutations == 1

    calls_before = notifier.external_calls
    second = await process_callback_job(second_job, handler=_core(broker, notifier))

    assert second["status"] == "discarded"
    # The single most important number in this file.
    assert broker.mutations == 1
    # A stale binding must not even answer the callback query.
    assert notifier.external_calls == calls_before

    row = await load_job(second_job)
    assert row is not None
    assert row.state == "discarded"
    assert row.outcome == "nonce_replay"


@pytest.mark.asyncio
async def test_an_unverified_send_is_a_succeeded_job_and_is_never_replayed(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R2 — ``handled=True, results=['unverified']`` must not re-enter the core."""
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="unverif1234", symbol="UNVKR")
    job_id = await _queue(inbox_cleanup, group)

    broker = _BrokerCounter(outcome="unverified")
    notifier = FakeNotifier()
    result = await process_callback_job(job_id, handler=_core(broker, notifier))

    assert result["status"] == "succeeded"
    assert broker.mutations == 1

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.attempt_count == 1

    # Re-delivering the same job must not run the core again.
    again = await process_callback_job(job_id, handler=_core(broker, notifier))
    assert again["status"] == "not_claimable"
    assert broker.mutations == 1


@pytest.mark.asyncio
async def test_a_guard_blocked_rejection_is_terminal_not_a_retry(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 13 — an explicit rejection never re-queues or reorders."""
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="guarded1234", symbol="GRDKR")
    job_id = await _queue(inbox_cleanup, group)

    broker = _BrokerCounter(outcome="guard_blocked")
    result = await process_callback_job(job_id, handler=_core(broker, FakeNotifier()))

    assert result["status"] == "succeeded"
    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.state != "retry_wait"
    assert row.attempt_count == 1


@pytest.mark.asyncio
async def test_a_generic_internal_error_after_core_entry_dead_letters(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R1 blocker 2 / R2 — an ambiguous failure is never replayed automatically.

    The callback core swallows every exception into ``internal_error``,
    including one raised *after* a submission. That string is not evidence
    that the broker leg never started, so the only safe move is to stop, scrub
    the authority, and make an operator re-issue the card.
    """
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="ambig123456", symbol="AMBKR")
    job_id = await _queue(inbox_cleanup, group)

    entries: list[int] = []

    async def _ambiguous(*, service, proposal_id, now, **kwargs):
        entries.append(1)
        raise RuntimeError("the broker leg may or may not have gone out")

    notifier = FakeNotifier()
    result = await process_callback_job(
        job_id,
        handler=functools.partial(
            handle_normalized_callback, revalidate_fn=_ambiguous, notifier=notifier
        ),
    )

    assert entries == [1]
    assert result["status"] == "dead_letter"

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "dead_letter"
    assert row.error_class == "handler_ambiguous"
    assert row.attempt_count == 1, "an ambiguous failure must not spend three attempts"
    # Authority is gone; a replay is impossible even by hand.
    for field in ("callback_query_id", "chat_id", "telegram_user_id", "nonce"):
        assert getattr(row, field) is None, field

    # And nothing re-enters the core afterwards.
    again = await process_callback_job(
        job_id,
        handler=functools.partial(
            handle_normalized_callback, revalidate_fn=_ambiguous, notifier=notifier
        ),
    )
    assert again["status"] == "not_claimable"
    assert entries == [1]


@pytest.mark.asyncio
async def test_repeated_pre_entry_crashes_exhaust_the_attempt_budget(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """RED item 14 — the third poisoned attempt dead-letters and scrubs.

    The crash here happens **before** the callback core is entered, which the
    inbox can prove from ``handler_entered_at``. That is the only crash class
    that may be re-run at all; see
    ``test_a_crash_inside_the_core_is_ambiguous_and_never_re_invoked``.
    """
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.contracts import (
        MAX_ATTEMPTS,
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="poison12345", symbol="PSNKR")
    job_id = await _queue(inbox_cleanup, group)
    broker = _BrokerCounter()
    entries: list[int] = []

    class _ProcessDied(BaseException):
        """Not an ``Exception``: nothing in the callback core can swallow this."""

    def _die(*args, **kwargs):
        entries.append(1)
        raise _ProcessDied

    original = worker_module.resolve_notifier
    worker_module.resolve_notifier = _die
    try:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            with pytest.raises(_ProcessDied):
                await process_callback_job(
                    job_id,
                    handler=_core(broker, FakeNotifier()),
                    claimable_states=RECOVERY_CLAIMABLE_STATES,
                )
            row = await load_job(job_id)
            assert row is not None
            assert row.attempt_count == attempt
            assert row.state == "processing"
            assert row.handler_entered_at is None, "crash was not pre-entry"
            # A crashed worker leaves no lock behind; age the row past the
            # recovery scan filter so the next tick can reclaim it.
            async with AsyncSessionLocal() as session:
                await shape_owned_callback_inbox_row(
                    session, job_id, started_at=now_kst() - timedelta(hours=6)
                )
                await session.commit()

        assert len(entries) == MAX_ATTEMPTS
        final = await process_callback_job(
            job_id,
            handler=_core(broker, FakeNotifier()),
            claimable_states=RECOVERY_CLAIMABLE_STATES,
        )
    finally:
        worker_module.resolve_notifier = original

    assert final["status"] == "dead_letter"
    assert len(entries) == MAX_ATTEMPTS, "a dead-lettered job was picked up again"
    assert broker.mutations == 0

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "dead_letter"
    assert row.error_class == "attempts_exhausted"
    for field in (
        "callback_query_id",
        "chat_id",
        "message_id",
        "telegram_user_id",
        "nonce",
        "action",
        "subject_short",
        "dispatch_attempt_id",
        "membership_revision",
        "membership_digest",
    ):
        assert getattr(row, field) is None, field


@pytest.mark.asyncio
async def test_a_crash_inside_the_core_is_ambiguous_and_never_re_invoked(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R2 — once the core is entered, a crash is unsafe to replay, full stop.

    The callback transaction rolls back, so the nonce reads unconsumed and the
    published binding still validates. Re-running would look legal and would
    submit a second time. ``handler_entered_at`` is the durable marker that
    makes the difference visible after the process is gone.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="incore12345", symbol="INCKR")
    job_id = await _queue(inbox_cleanup, group)
    broker = _BrokerCounter()

    class _ProcessDied(BaseException):
        pass

    async def _die_inside(normalized, **kwargs):
        raise _ProcessDied

    with pytest.raises(_ProcessDied):
        await process_callback_job(job_id, handler=_die_inside)

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "processing"
    assert row.handler_entered_at is not None
    assert row.handler_completed_at is None

    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(
            session, job_id, started_at=now_kst() - timedelta(hours=6)
        )
        await session.commit()

    reclaimed = await process_callback_job(
        job_id,
        handler=_core(broker, FakeNotifier()),
        claimable_states=RECOVERY_CLAIMABLE_STATES,
    )
    assert reclaimed["status"] == "dead_letter"
    assert broker.mutations == 0, "an ambiguous crash was replayed into the core"

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "dead_letter"
    assert row.error_class == "handler_ambiguous"
    assert row.nonce is None and row.chat_id is None


@pytest.mark.asyncio
async def test_a_pre_core_failure_is_the_only_thing_that_schedules_a_retry(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R2 — ``retry_wait`` requires proof the core was never entered."""
    from app.services.order_proposals.callback_inbox import worker as worker_module
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="precore1234", symbol="PRCKR")
    job_id = await _queue(inbox_cleanup, group)
    broker = _BrokerCounter()

    def _explode(*args, **kwargs):
        raise RuntimeError("could not resolve the notifier")

    original = worker_module.resolve_notifier
    worker_module.resolve_notifier = _explode
    try:
        result = await process_callback_job(
            job_id, handler=_core(broker, FakeNotifier())
        )
    finally:
        worker_module.resolve_notifier = original

    assert broker.mutations == 0
    assert result["status"] == "retry_scheduled"

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "retry_wait"
    assert row.error_class == "pre_core_failure"
    assert row.attempt_count == 1
    assert row.available_at > row.received_at
    # Reconstruction fields survive a retry — the row must stay runnable.
    assert row.nonce is not None
    assert row.chat_id is not None
    assert row.membership_digest is not None


@pytest.mark.asyncio
async def test_a_retry_wait_row_is_not_claimable_before_its_backoff_elapses(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="backoff1234", symbol="BKFKR")
    job_id = await _queue(inbox_cleanup, group)
    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(
            session,
            job_id,
            state="retry_wait",
            attempt_count=1,
            error_class="pre_core_failure",
            available_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await session.commit()

    broker = _BrokerCounter()
    result = await process_callback_job(job_id, handler=_core(broker, FakeNotifier()))
    assert result["status"] == "not_claimable"
    assert broker.mutations == 0


@pytest.mark.asyncio
async def test_a_failed_terminal_commit_is_repaired_not_re_executed(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """R2 — a lost terminal/scrub commit must never re-invoke the handler.

    The worker records the handler's verdict in its own commit before
    applying the terminal state and the scrub. If the second commit is lost,
    the row is still ``processing`` but it carries ``handler_completed_at``
    plus the decided terminal state, and recovery finishes the paperwork
    instead of re-running an order-adjacent handler.
    """
    from app.services.order_proposals.callback_inbox.contracts import (
        RECOVERY_CLAIMABLE_STATES,
    )
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="repair12345", symbol="RPRKR")
    job_id = await _queue(inbox_cleanup, group)
    broker = _BrokerCounter()

    # Reproduce the exact post-crash shape: verdict recorded, terminal not
    # applied, no lock held (the worker's backend is gone).
    async with AsyncSessionLocal() as session:
        await shape_owned_callback_inbox_row(
            session,
            job_id,
            state="processing",
            attempt_count=1,
            started_at=now_kst() - timedelta(hours=6),
            # All three durable facts, in causal order. R13: a verdict
            # without an entry is a shape the database now refuses, and
            # repair must never accept it either.
            handler_entered_at=now_kst() - timedelta(hours=6),
            handler_completed_at=now_kst() - timedelta(hours=6),
            terminal_state_pending="succeeded",
            outcome="approved",
        )
        await session.commit()

    result = await process_callback_job(
        job_id,
        handler=_core(broker, FakeNotifier()),
        claimable_states=RECOVERY_CLAIMABLE_STATES,
    )

    assert result["status"] == "succeeded"
    assert broker.mutations == 0, "recovery re-ran a completed handler"

    row = await load_job(job_id)
    assert row is not None
    assert row.state == "succeeded"
    assert row.outcome == "approved"
    assert row.terminal_state_pending is None
    assert row.attempt_count == 1, "a scrub repair must not spend an attempt"
    assert row.nonce is None and row.chat_id is None


@pytest.mark.asyncio
async def test_a_stored_envelope_that_cannot_be_rebuilt_is_discarded(
    _bootstrap_test_schema, db_session, inbox_cleanup: list[uuid.UUID]
) -> None:
    """Corruption is a fail-closed discard, never a guess or a retry."""
    from app.services.order_proposals.callback_inbox.worker import (
        process_callback_job,
    )

    group = await seed_proposal(db_session, nonce="corrupt1234", symbol="CRPKR")
    job_id = await _queue(inbox_cleanup, group)
    before = await load_job(job_id)
    assert before is not None
    assert before.state == "pending"
    assert before.subject_short is not None
    async with AsyncSessionLocal() as session:
        corrupted = await degrade_owned_callback_subject_short(session, job_id)
        assert corrupted.state == "pending"
        assert corrupted.subject_short == "zzzzzzzz"
        await session.commit()

    broker = _BrokerCounter()
    result = await process_callback_job(job_id, handler=_core(broker, FakeNotifier()))
    assert result["status"] == "discarded"
    assert broker.mutations == 0
    row = await load_job(job_id)
    assert row is not None
    assert row.error_class == "envelope_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "subject_short", "accepted"),
    [
        *(
            pytest.param(state, "deadbeef", True, id=f"allows-{state}")
            for state in _SUBJECT_DEGRADER_ALLOWED_STATES
        ),
        *(
            pytest.param(state, "deadbeef", False, id=f"terminal-{state}")
            for state in sorted(TERMINAL_STATES)
        ),
        pytest.param("unknown", "deadbeef", False, id="unknown-state"),
        pytest.param(None, "deadbeef", False, id="none-state"),
        pytest.param(0, "deadbeef", False, id="non-string-state"),
        pytest.param("pending", None, False, id="none-subject"),
        pytest.param("pending", 8, False, id="non-string-subject"),
        pytest.param(
            "pending",
            _SubjectShortSubclass("deadbeef"),
            False,
            id="string-subclass-subject",
        ),
        pytest.param("pending", "zzzzzzzz", False, id="invalid-subject"),
    ],
)
async def test_test_owned_subject_degrader_accepts_only_reconstructable_recovery_rows(
    inbox_cleanup: list[uuid.UUID],
    state: object,
    subject_short: object,
    *,
    accepted: bool,
) -> None:
    """The test-only corruption seam has no state or type escape hatch."""

    class _SubjectShortSession:
        def __init__(self) -> None:
            self.row = SimpleNamespace(state=state, subject_short=subject_short)
            self.scalar_calls = 0
            self.flush_calls = 0

        async def scalar(self, _statement: object) -> SimpleNamespace:
            self.scalar_calls += 1
            return self.row

        async def flush(self) -> None:
            self.flush_calls += 1

    assert RECOVERY_CLAIMABLE_STATES == frozenset(_SUBJECT_DEGRADER_ALLOWED_STATES)
    job_id = uuid.uuid4()
    inbox_cleanup.append(job_id)
    session = _SubjectShortSession()
    original_state = session.row.state
    original_subject_short = session.row.subject_short

    if accepted:
        row = await degrade_owned_callback_subject_short(
            session,  # type: ignore[arg-type] - test double observes only this seam
            job_id,
        )
        from app.services.order_proposals.callback_inbox.worker import _SUBJECT_SHORT

        assert row is session.row
        assert session.row.subject_short == "zzzzzzzz"
        assert _SUBJECT_SHORT.fullmatch(session.row.subject_short) is None  # noqa: SLF001
        assert session.scalar_calls == 1
        assert session.flush_calls == 1
    else:
        with pytest.raises(ValueError):
            await degrade_owned_callback_subject_short(
                session,  # type: ignore[arg-type] - test double observes only this seam
                job_id,
            )
        assert session.scalar_calls == 1
        assert session.flush_calls == 0
        assert session.row.state is original_state
        assert session.row.subject_short is original_subject_short
