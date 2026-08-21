"""The only writer for ``review.telegram_callback_inbox``.

Every state transition the durable inbox performs lives here, so the scrub,
the attempt accounting and the claim rules exist in exactly one place. The
service flushes; the caller commits, because *which transaction* a write lands
in is the whole safety argument (see :mod:`.worker`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.telegram_callback_inbox import TelegramCallbackInboxJob
from app.services.order_proposals.callback_inbox.contracts import (
    MAX_ATTEMPTS,
    PROCESSING_STALE_AFTER_SECONDS,
    RETRY_BACKOFF_SECONDS,
    SCRUBBED_ON_TERMINAL,
    ErrorClass,
    InboxState,
    normalize_outcome,
)
from app.services.order_proposals.callback_inbox.repository import (
    CallbackInboxRepository,
)


class CallbackInboxConflict(Exception):
    """A stored row exists for this delivery but describes a different call."""


class RetryAuthorityRefused(Exception):
    """The durable row does not prove the callback core was never entered.

    Retry authority is not something a caller can assert; it is something the
    row has to still be able to show. Once ``handler_entered_at`` is
    committed, no argument, return value or exception can buy a replay --
    because a handler that already reached the broker would be able to make
    exactly the same claim as one that never started.
    """


@dataclass(frozen=True, slots=True)
class ClaimDecision:
    """What a claimant may do with a row it has just locked."""

    #: ``"run"`` | ``"repair"`` | ``"ambiguous"`` | ``"exhausted"`` | ``"skip"``
    action: str
    reason: str | None = None


class CallbackInboxService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = CallbackInboxRepository(session)

    # -- ingress ---------------------------------------------------------

    async def enqueue(
        self,
        *,
        update_digest: str,
        now: datetime,
        callback_query_id: str | None,
        chat_id: str,
        message_id: int | None,
        telegram_user_id: str | None,
        action: str,
        subject_short: str,
        dispatch_attempt_id: uuid.UUID,
        membership_revision: int,
        membership_digest: str,
        nonce: str,
    ) -> TelegramCallbackInboxJob:
        return await self._repo.insert(
            job_id=uuid.uuid4(),
            update_digest=update_digest,
            state=InboxState.PENDING.value,
            attempt_count=0,
            max_attempts=MAX_ATTEMPTS,
            received_at=now,
            available_at=now,
            callback_query_id=callback_query_id,
            chat_id=chat_id,
            message_id=message_id,
            telegram_user_id=telegram_user_id,
            action=action,
            subject_short=subject_short,
            dispatch_attempt_id=dispatch_attempt_id,
            membership_revision=membership_revision,
            membership_digest=membership_digest,
            nonce=nonce,
        )

    async def get_by_update_digest(
        self, update_digest: str
    ) -> TelegramCallbackInboxJob | None:
        return await self._repo.get_by_update_digest(update_digest)

    async def get(
        self, job_id: uuid.UUID, *, for_update: bool = False
    ) -> TelegramCallbackInboxJob | None:
        return await self._repo.get_by_job_id(job_id, for_update=for_update)

    # -- claim -----------------------------------------------------------

    def classify_claim(
        self,
        row: TelegramCallbackInboxJob,
        *,
        now: datetime,
        claimable_states: frozenset[str],
    ) -> ClaimDecision:
        """Decide what a claimant holding the lock may do with this row.

        The ordering matters. A row that already carries a verdict is
        *repaired*, never re-executed; a row that entered the core without
        producing one is *ambiguous*, and re-running it could submit twice.
        Only a row that provably never reached the core may run.
        """
        if row.state not in claimable_states:
            return ClaimDecision("skip", "state")

        if row.state == InboxState.PROCESSING.value:
            # A repair finalises without re-running, so it needs the whole
            # causal chain: entered, then completed, then a verdict. Anything
            # short of that is ambiguous, never a success.
            if (
                row.handler_entered_at is not None
                and row.handler_completed_at is not None
                and row.terminal_state_pending is not None
            ):
                return ClaimDecision("repair")
            if (
                row.handler_entered_at is not None
                or row.handler_completed_at is not None
                or row.terminal_state_pending is not None
            ):
                return ClaimDecision("ambiguous")
            if row.started_at is None or row.started_at > now - timedelta(
                seconds=PROCESSING_STALE_AFTER_SECONDS
            ):
                return ClaimDecision("skip", "not_stale")
        elif row.state == InboxState.RETRY_WAIT.value and row.available_at > now:
            return ClaimDecision("skip", "not_due")

        if row.attempt_count >= row.max_attempts:
            return ClaimDecision("exhausted")
        return ClaimDecision("run")

    async def begin_attempt(
        self, row: TelegramCallbackInboxJob, *, now: datetime
    ) -> bool:
        """Spend one attempt and re-arm the row for a fresh execution.

        Committed by the caller *before* the handler runs, so a process that
        dies mid-handler has already paid for the attempt and a crash loop
        converges on the dead-letter rather than spinning forever.

        Conditional, for the same reason ``schedule_retry`` is: this clears
        the durable entry markers, and it may only do so on a row the database
        still agrees has none. Returns ``False`` if the row moved underneath.
        """
        return await self._repo.try_conditional_update(
            job_id=row.job_id,
            predicate=and_(
                TelegramCallbackInboxJob.attempt_count == row.attempt_count,
                TelegramCallbackInboxJob.handler_entered_at.is_(None),
                TelegramCallbackInboxJob.state.in_(
                    [
                        InboxState.PENDING.value,
                        InboxState.RETRY_WAIT.value,
                        InboxState.PROCESSING.value,
                    ]
                ),
            ),
            # Nothing is cleared: the predicate has already established that
            # this row has no entry marker, and the database's causal CHECK
            # means it can therefore have no completion or verdict either.
            # Markers are monotonic -- no API may write one back to NULL.
            values={
                "state": InboxState.PROCESSING.value,
                "attempt_count": row.attempt_count + 1,
                "started_at": now,
            },
        )

    async def mark_handler_entered(
        self, row: TelegramCallbackInboxJob, *, now: datetime
    ) -> TelegramCallbackInboxJob:
        """Record that the callback core is about to be invoked.

        Committed on its own. It is the only durable difference between "the
        process died before the mutating region" (safe to re-run) and "the
        process died inside it" (ambiguous, never re-run).
        """
        return await self._repo.update(row, handler_entered_at=now)

    async def record_handler_verdict(
        self,
        row: TelegramCallbackInboxJob,
        *,
        now: datetime,
        terminal_state: str,
        outcome: object,
        error_class: str | None,
    ) -> TelegramCallbackInboxJob:
        """Persist the decided outcome while the row is still ``processing``.

        Committed before the terminal state is applied. If the *next* commit
        is lost, recovery finds a row that says "the handler finished and this
        is what it decided" and finishes the paperwork instead of re-running
        an order-adjacent handler.
        """
        return await self._repo.update(
            row,
            handler_completed_at=now,
            terminal_state_pending=terminal_state,
            outcome=normalize_outcome(outcome),
            error_class=error_class,
        )

    # -- terminal --------------------------------------------------------

    async def finalize(
        self,
        row: TelegramCallbackInboxJob,
        *,
        now: datetime,
        terminal_state: str,
        outcome: object = None,
        error_class: str | None = None,
        keep_recorded_outcome: bool = False,
    ) -> TelegramCallbackInboxJob:
        """Apply a terminal state and scrub every authority/PII column.

        The scrub is not optional and not conditional: the same DB CHECK that
        rejects an unscrubbed terminal row would reject this write too, so a
        future edit that "temporarily" keeps a nonce fails loudly.
        """
        fields: dict[str, Any] = dict.fromkeys(SCRUBBED_ON_TERMINAL)
        fields.update(
            state=terminal_state,
            terminal_state_pending=None,
            finished_at=now,
        )
        if not keep_recorded_outcome:
            fields["outcome"] = normalize_outcome(outcome)
            fields["error_class"] = error_class
        return await self._repo.update(row, **fields)

    async def schedule_retry(
        self,
        row: TelegramCallbackInboxJob,
        *,
        now: datetime,
    ) -> None:
        """Park a provably-unexecuted job until its backoff elapses.

        There is deliberately no ``error_class``/``outcome`` parameter.
        ``retry_wait`` has exactly one meaning -- a failure that provably
        never reached the mutating region -- so the vocabulary is written
        here, not accepted from a caller. Validating an argument would be
        weaker: the parameter itself is the vulnerability, because every
        future caller would have to be reviewed for it. A job that never
        entered the core also produced no verdict, so ``outcome`` stays NULL.
        The database enforces the same rule (``ck_..._retry_vocabulary``).

        The precondition is the whole point, and it is checked **in the
        database**: the row must still be a ``processing`` row that has not
        entered the core, has not produced a verdict, and has no terminal
        state waiting to be applied. Anything else raises
        :class:`RetryAuthorityRefused` and writes nothing at all -- in
        particular it does not clear ``handler_entered_at``, which would
        destroy the only evidence that a replay is unsafe.

        Note there is nothing to clear on the success path either: the
        predicate has already established that all three markers are NULL.
        """
        index = min(max(row.attempt_count, 1), len(RETRY_BACKOFF_SECONDS)) - 1
        granted = await self._repo.try_conditional_update(
            job_id=row.job_id,
            predicate=and_(
                TelegramCallbackInboxJob.state == InboxState.PROCESSING.value,
                TelegramCallbackInboxJob.handler_entered_at.is_(None),
                TelegramCallbackInboxJob.handler_completed_at.is_(None),
                TelegramCallbackInboxJob.terminal_state_pending.is_(None),
            ),
            values={
                "state": InboxState.RETRY_WAIT.value,
                "available_at": now + timedelta(seconds=RETRY_BACKOFF_SECONDS[index]),
                "outcome": None,
                "error_class": ErrorClass.PRE_CORE_FAILURE.value,
            },
        )
        if not granted:
            raise RetryAuthorityRefused(str(row.job_id))

    # -- recovery scan ---------------------------------------------------

    async def claimable_job_ids(self, *, now: datetime, limit: int) -> list[uuid.UUID]:
        return await self._repo.claimable_job_ids(
            now=now,
            stale_before=now - timedelta(seconds=PROCESSING_STALE_AFTER_SECONDS),
            limit=limit,
        )

    async def backlog(self, *, now: datetime) -> dict[str, Any]:
        """Aggregate-only backlog. Counts and one age; never an identifier."""
        counts = await self._repo.counts_by_state()
        oldest = await self._repo.oldest_pending_received_at()
        return {
            "pending": counts.get(InboxState.PENDING.value, 0),
            "processing": counts.get(InboxState.PROCESSING.value, 0),
            "retry_wait": counts.get(InboxState.RETRY_WAIT.value, 0),
            "dead_letter": counts.get(InboxState.DEAD_LETTER.value, 0),
            "oldest_pending_age_seconds": (
                None if oldest is None else round((now - oldest).total_seconds(), 3)
            ),
        }

    # -- tests only ------------------------------------------------------

    async def force_state_for_test(
        self, job_id: uuid.UUID, **fields: Any
    ) -> TelegramCallbackInboxJob:
        """Put a row into an exact shape a crash would have left behind.

        Test-only seam. Production code never reaches it: reproducing "a
        worker died holding this row" any other way would need a real second
        process, and a fake row is the only way to pin the *classification*
        rules against every crash shape.
        """
        row = await self._repo.get_by_job_id(job_id)
        if row is None:
            raise LookupError(str(job_id))
        return await self._repo.update(row, **fields)


__all__ = [
    "CallbackInboxConflict",
    "CallbackInboxService",
    "ClaimDecision",
    "RetryAuthorityRefused",
]
