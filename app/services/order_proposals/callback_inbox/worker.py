"""Execute one durable callback job, exactly once.

The safety argument, in order:

**The lock, not the row, is the claim.** Nothing happens until this process
holds the job's PostgreSQL session advisory lock on its own connection. Two
tasks for one job therefore invoke the handler once; a live worker is never
overtaken because a lease timestamp expired.

**Attempts are paid for before the work.** ``processing`` and
``attempt_count + 1`` are committed before the handler runs, so a process that
dies mid-handler has already spent its attempt and a crash loop converges on
the dead-letter instead of spinning.

**Entering the core is a durable fact.** ``handler_entered_at`` is committed
immediately before the call. That single marker is what separates "died before
the mutating region" (safe to re-run) from "died inside it" (never re-run).
It has to be durable because the callback core's transaction *rolls back* on
a crash: afterwards the nonce reads unconsumed and the published binding still
validates, so a re-run would look perfectly legal and submit a second time.

**The verdict is recorded before it is applied.** ``handler_completed_at`` +
``terminal_state_pending`` land in their own commit, so a lost terminal commit
is repaired by recovery rather than replayed through an order-adjacent
handler.

**Only a provably unexecuted failure retries.** Not ``internal_error`` -- the
core funnels every exception into that string, including one raised after a
submission -- and never an explicit rejection or an ``unverified`` send, which
are outcomes the proposal/order state already owns.

**Processing time is the clock.** ``now`` is sampled when execution starts,
not when the update arrived, so queue delay can never extend ``valid_until``,
the loss-cut confirmation window, or a batch TTL.
"""

from __future__ import annotations

import logging
import re
import uuid
from collections.abc import Callable
from datetime import datetime
from typing import Any

from app.core.config import settings
from app.core.db import AsyncSessionLocal
from app.core.timezone import now_kst
from app.services.order_proposals.callback_inbox.contracts import (
    TERMINAL_STATE_STATUS,
    ErrorClass,
    InboxState,
    WorkerStatus,
    job_advisory_lock_key,
)
from app.services.order_proposals.callback_inbox.locks import (
    JobAdvisoryLock,
    job_advisory_lock,
)
from app.services.order_proposals.callback_inbox.observability import (
    annotate,
    build_worker_span_data,
    log_job_event,
    worker_transaction,
)
from app.services.order_proposals.callback_inbox.service import (
    CallbackInboxService,
    RetryAuthorityRefused,
)
from app.services.order_proposals.dispatch_contract import CallbackEnvelope
from app.services.order_proposals.telegram_callback import (
    NormalizedCallback,
    handle_normalized_callback,
)

logger = logging.getLogger(__name__)

Clock = Callable[[], datetime]

#: Sampled at execution start, never at receive time.
DEFAULT_CLOCK: Clock = now_kst

_SUBJECT_SHORT = re.compile(r"[0-9a-f]{8}")
_NONCE = re.compile(r"[A-Za-z0-9_-]+")
_DIGEST = re.compile(r"[A-Za-z0-9_-]{12}")
_ACTIONS = frozenset({"op", "dn", "lc", "vc", "ba"})


def resolve_notifier() -> Any:
    """Resolve the shared Telegram notifier.

    A module-level indirection so a test can make notifier resolution fail and
    exercise the pre-core failure class without reaching into the monitoring
    package.
    """
    from app.monitoring.trade_notifier.notifier import get_trade_notifier

    return get_trade_notifier()


class EnvelopeInvalid(Exception):
    """A stored row no longer rebuilds into a valid callback."""


class PreCoreFailure(Exception):
    """The only grant of retry authority, and the worker owns it.

    Raised exclusively from the phase that runs *before* the callback core is
    entered, so raising it is itself the proof that nothing has mutated. A
    handler cannot produce it: by the time a handler runs, this phase is over,
    and an exception escaping a handler is caught as
    :class:`ErrorClass.HANDLER_EXCEPTION` instead. The durable
    ``handler_entered_at`` marker is checked again in the database before the
    retry is written, so even this exception cannot replay a job whose core
    was reached.
    """


def rebuild_normalized(row: Any) -> NormalizedCallback:
    """Rebuild the exact envelope the inline path would have produced.

    Validated against the same shapes the live parser enforces, so a corrupted
    or hand-edited row fails closed instead of being guessed at.
    """
    if row.action not in _ACTIONS:
        raise EnvelopeInvalid("action")
    if not isinstance(row.subject_short, str) or not _SUBJECT_SHORT.fullmatch(
        row.subject_short
    ):
        raise EnvelopeInvalid("subject_short")
    if not isinstance(row.membership_digest, str) or not _DIGEST.fullmatch(
        row.membership_digest
    ):
        raise EnvelopeInvalid("membership_digest")
    if not isinstance(row.nonce, str) or not _NONCE.fullmatch(row.nonce):
        raise EnvelopeInvalid("nonce")
    if not isinstance(row.membership_revision, int) or row.membership_revision <= 0:
        raise EnvelopeInvalid("membership_revision")
    if not isinstance(row.dispatch_attempt_id, uuid.UUID):
        raise EnvelopeInvalid("dispatch_attempt_id")
    if not isinstance(row.chat_id, str) or not row.chat_id.strip():
        raise EnvelopeInvalid("chat_id")

    chat_id: Any = row.chat_id
    candidate = chat_id[1:] if chat_id.startswith("-") else chat_id
    if candidate.isdigit():
        chat_id = int(chat_id)

    return NormalizedCallback(
        callback_query_id=row.callback_query_id,
        chat_id=chat_id,
        chat_id_key=row.chat_id,
        message_id=row.message_id,
        telegram_user_id=row.telegram_user_id,
        callback=CallbackEnvelope(
            action=row.action,
            subject_short=row.subject_short,
            attempt_id=row.dispatch_attempt_id,
            membership_revision=row.membership_revision,
            membership_digest=row.membership_digest,
            nonce=row.nonce,
        ),
    )


def classify_verdict(result: Any) -> tuple[str, str | None, object]:
    """Map a callback-core result to ``(state, error_class)``.

    Every branch here is terminal. Once the core has been entered there is no
    return value that can send the job back round, because a handler that
    already reached the broker can return exactly what an untouched one would
    -- see :data:`IGNORED_HANDLER_RETRY_KEYS`.

    The one asymmetry worth stating plainly: ``handled=True`` is success even
    when a rung came back ``unverified``. An ambiguous *send* is already
    modelled by the proposal/order state machine, which owns it; re-running
    the callback would not resolve it and could duplicate it.
    """
    if not isinstance(result, dict):
        return (
            InboxState.DEAD_LETTER.value,
            ErrorClass.HANDLER_AMBIGUOUS.value,
            None,
        )
    reason = result.get("reason")
    if result.get("handled"):
        return InboxState.SUCCEEDED.value, None, reason
    if reason == "internal_error":
        # Ambiguous by construction; see this module's docstring.
        return (
            InboxState.DEAD_LETTER.value,
            ErrorClass.HANDLER_AMBIGUOUS.value,
            reason,
        )
    return InboxState.DISCARDED.value, None, reason


async def process_callback_job(
    job_id: uuid.UUID | str,
    *,
    now_fn: Clock | None = None,
    session_factory: Callable[[], Any] | None = None,
    claimable_states: frozenset[str] | None = None,
    handler: Callable[..., Any] | None = None,
    lock: JobAdvisoryLock | None = None,
    **handler_kwargs: Any,
) -> dict[str, str]:
    """Run one job to a terminal state, a scheduled retry, or not at all."""
    from app.services.order_proposals.callback_inbox.contracts import (
        WORKER_CLAIMABLE_STATES,
    )

    resolved_id = job_id if isinstance(job_id, uuid.UUID) else uuid.UUID(str(job_id))
    states = claimable_states or WORKER_CLAIMABLE_STATES
    clock = now_fn or DEFAULT_CLOCK
    # Resolved by name at call time, never frozen into a default argument, so
    # the production seam is the module attribute a test can observe -- and so
    # there is exactly one place the real callback core is named.
    execute = handler or handle_normalized_callback

    with worker_transaction(resolved_id) as span:
        async with job_advisory_lock(
            job_advisory_lock_key(resolved_id), lock=lock
        ) as acquired:
            if not acquired:
                return _result(resolved_id, WorkerStatus.LOCK_CONTENDED.value)
            status, span_data = await _run_locked(
                resolved_id,
                clock=clock,
                session_factory=session_factory or AsyncSessionLocal,
                claimable_states=states,
                handler=execute,
                handler_kwargs=handler_kwargs,
            )
        annotate(span, span_data)
    return _result(resolved_id, status)


async def _run_locked(
    job_id: uuid.UUID,
    *,
    clock: Clock,
    session_factory: Callable[[], Any],
    claimable_states: frozenset[str],
    handler: Callable[..., Any],
    handler_kwargs: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    now = clock()

    async with session_factory() as session:
        service = CallbackInboxService(session)
        row = await service.get(job_id)
        if row is None:
            return WorkerStatus.NOT_FOUND.value, {}

        decision = service.classify_claim(
            row, now=now, claimable_states=claimable_states
        )
        queue_delay = (now - row.received_at).total_seconds()

        if decision.action == "skip":
            return WorkerStatus.NOT_CLAIMABLE.value, {}

        if decision.action == "repair":
            # The handler already finished; only the paperwork was lost. No
            # attempt is spent and the core is not touched.
            terminal_state = str(row.terminal_state_pending)
            await service.finalize(
                row,
                now=now,
                terminal_state=terminal_state,
                keep_recorded_outcome=True,
            )
            await session.commit()
            return _finish(
                job_id,
                terminal_state,
                attempt_count=row.attempt_count,
                queue_delay=queue_delay,
                outcome=row.outcome,
                error_class=row.error_class,
                event="callback_job_repaired",
            )

        if decision.action == "ambiguous":
            return await _terminate(
                session,
                service,
                row,
                job_id=job_id,
                now=now,
                queue_delay=queue_delay,
                terminal_state=InboxState.DEAD_LETTER.value,
                error_class=ErrorClass.HANDLER_AMBIGUOUS.value,
                outcome=None,
                attempt_count=row.attempt_count,
                event="callback_job_ambiguous",
            )

        if decision.action == "exhausted":
            return await _terminate(
                session,
                service,
                row,
                job_id=job_id,
                now=now,
                queue_delay=queue_delay,
                terminal_state=InboxState.DEAD_LETTER.value,
                error_class=ErrorClass.ATTEMPTS_EXHAUSTED.value,
                outcome=None,
                attempt_count=row.attempt_count,
                event="callback_job_attempts_exhausted",
            )

        # Pay for the attempt before doing the work. Conditional: if the row
        # moved underneath us, we are not the claimant we thought we were.
        attempt_count = row.attempt_count + 1
        if not await service.begin_attempt(row, now=now):
            await session.rollback()
            return WorkerStatus.NOT_CLAIMABLE.value, {}
        await session.commit()
        row = await service.get(job_id)
        if row is None:  # pragma: no cover - defensive
            return WorkerStatus.NOT_FOUND.value, {}

        try:
            normalized = rebuild_normalized(row)
        except EnvelopeInvalid:
            return await _terminate(
                session,
                service,
                row,
                job_id=job_id,
                now=now,
                queue_delay=queue_delay,
                terminal_state=InboxState.DISCARDED.value,
                error_class=ErrorClass.ENVELOPE_INVALID.value,
                outcome=None,
                attempt_count=attempt_count,
                event="callback_job_envelope_invalid",
            )

        # Re-authorise the stored envelope against *current* settings.
        if (
            normalized.chat_id_key
            not in settings.order_proposals_telegram_chat_allowlist
        ):
            return await _terminate(
                session,
                service,
                row,
                job_id=job_id,
                now=now,
                queue_delay=queue_delay,
                terminal_state=InboxState.DISCARDED.value,
                error_class=ErrorClass.CHAT_REVOKED.value,
                outcome="chat_not_allowed",
                attempt_count=attempt_count,
                event="callback_job_chat_revoked",
            )

        try:
            try:
                notifier = resolve_notifier()
            except Exception as exc:  # noqa: BLE001 - provably before the core
                raise PreCoreFailure("notifier unavailable") from exc
        except PreCoreFailure:
            # The only path to a retry. The database is still asked to confirm
            # independently that the core was never entered before one is
            # written; if it disagrees, this becomes ambiguous, not a replay.
            try:
                await service.schedule_retry(row, now=now)
            except RetryAuthorityRefused:
                await session.rollback()
                refreshed = await service.get(job_id)
                return await _terminate(
                    session,
                    service,
                    refreshed if refreshed is not None else row,
                    job_id=job_id,
                    now=now,
                    queue_delay=queue_delay,
                    terminal_state=InboxState.DEAD_LETTER.value,
                    error_class=ErrorClass.HANDLER_AMBIGUOUS.value,
                    outcome=None,
                    attempt_count=attempt_count,
                    event="callback_job_retry_refused",
                )
            await session.commit()
            return _finish(
                job_id,
                InboxState.RETRY_WAIT.value,
                attempt_count=attempt_count,
                queue_delay=queue_delay,
                outcome=None,
                error_class=ErrorClass.PRE_CORE_FAILURE.value,
                event="callback_job_retry_scheduled",
            )

        # Durable "we are about to enter the core", in its own commit. Every
        # line above this one is the pre-core region: a failure there is the
        # only thing that can earn a retry, and the database is asked to
        # confirm that independently before one is written.
        await service.mark_handler_entered(row, now=now)
        await session.commit()

        try:
            result = await handler(
                normalized,
                now=now,
                service_factory=AsyncSessionLocal,
                notifier=notifier,
                now_fn=clock,
                **handler_kwargs,
            )
        except Exception:  # noqa: BLE001 - the core promises never to raise
            terminal_state, error_class = (
                InboxState.DEAD_LETTER.value,
                ErrorClass.HANDLER_EXCEPTION.value,
            )
            return await _terminate(
                session,
                service,
                row,
                job_id=job_id,
                now=now,
                queue_delay=queue_delay,
                terminal_state=terminal_state,
                error_class=error_class,
                outcome=None,
                attempt_count=attempt_count,
                event="callback_job_handler_raised",
            )

        terminal_state, error_class, outcome = classify_verdict(result)

        # Record the verdict, then apply it. Two commits on purpose.
        await service.record_handler_verdict(
            row,
            now=now,
            terminal_state=terminal_state,
            outcome=outcome,
            error_class=error_class,
        )
        await session.commit()

        await service.finalize(
            row, now=now, terminal_state=terminal_state, keep_recorded_outcome=True
        )
        await session.commit()
        return _finish(
            job_id,
            terminal_state,
            attempt_count=attempt_count,
            queue_delay=queue_delay,
            outcome=outcome,
            error_class=error_class,
            event="callback_job_finished",
        )


async def _terminate(
    session: Any,
    service: CallbackInboxService,
    row: Any,
    *,
    job_id: uuid.UUID,
    now: datetime,
    queue_delay: float,
    terminal_state: str,
    error_class: str | None,
    outcome: object,
    attempt_count: int,
    event: str,
) -> tuple[str, dict[str, Any]]:
    await service.finalize(
        row,
        now=now,
        terminal_state=terminal_state,
        outcome=outcome,
        error_class=error_class,
    )
    await session.commit()
    return _finish(
        job_id,
        terminal_state,
        attempt_count=attempt_count,
        queue_delay=queue_delay,
        outcome=outcome,
        error_class=error_class,
        event=event,
    )


def _finish(
    job_id: uuid.UUID,
    state: str,
    *,
    attempt_count: int,
    queue_delay: float | None,
    outcome: object,
    error_class: str | None,
    event: str,
) -> tuple[str, dict[str, Any]]:
    log_job_event(
        f"order_proposals.telegram.{event}",
        job_id=job_id,
        state=state,
        attempt_count=attempt_count,
        queue_delay_seconds=queue_delay,
        outcome=outcome,
        error_class=error_class,
    )
    span_data = build_worker_span_data(
        job_id=job_id,
        state=state,
        attempt_count=attempt_count,
        queue_delay_seconds=queue_delay,
        outcome=outcome,
        error_class=error_class,
    )
    if state == InboxState.RETRY_WAIT.value:
        return WorkerStatus.RETRY_SCHEDULED.value, span_data
    return TERMINAL_STATE_STATUS[state], span_data


def _result(job_id: uuid.UUID, status: str) -> dict[str, str]:
    """The complete, allowlisted payload that may reach Redis."""
    from app.services.order_proposals.callback_inbox.contracts import WORKER_STATUSES

    if status not in WORKER_STATUSES:  # pragma: no cover - defensive
        raise ValueError("callback job status is not allowlisted")
    return {"status": status, "job_id": str(job_id)}


__all__ = [
    "DEFAULT_CLOCK",
    "EnvelopeInvalid",
    "PreCoreFailure",
    "classify_verdict",
    "process_callback_job",
    "rebuild_normalized",
    "resolve_notifier",
]
