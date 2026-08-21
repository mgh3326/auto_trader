"""W5 durable Telegram callback inbox (review schema).

PostgreSQL is the authority for an order-adjacent Telegram approval click.
TaskIQ carries nothing but an opaque job UUID, so losing Redis loses latency,
never a click.

All writes go through
``app.services.order_proposals.callback_inbox.service.CallbackInboxService``.

Data minimisation is a schema property, not a convention
--------------------------------------------------------
The raw Telegram ``Update`` is never stored. Only the fields the existing
``CallbackEnvelope`` needs, plus what the worker must re-gate on, live here --
and even those are NULLed the moment the job reaches a terminal state. Two
CHECK constraints make that mechanical rather than aspirational:

``telegram_callback_inbox_terminal_scrubbed``
    a terminal row (``succeeded``/``discarded``/``dead_letter``) must have
    every authority/PII column NULL;
``telegram_callback_inbox_active_reconstructable``
    an active row (``pending``/``processing``/``retry_wait``) must have every
    column the worker needs to rebuild the envelope explicitly non-NULL.

Both are written as ``CASE WHEN ... THEN ... ELSE true END`` over ``IS NULL``
/ ``IS NOT NULL`` predicates so no operand can evaluate to SQL ``UNKNOWN`` and
slip past the constraint.

What survives a terminal scrub is ``update_digest`` -- a one-way,
domain-separated identity digest that can dedupe a re-delivery but cannot
reconstruct the authority it was derived from -- plus a slug outcome label and
a closed-vocabulary error class.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    BigInteger,
    CheckConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.models.base import Base
from app.services.order_proposals.callback_inbox.contracts import (
    ACTIVE_REQUIRED_COLUMNS,
    ERROR_CLASSES,
    INBOX_STATES,
    MAX_ATTEMPTS,
    OUTCOME_LABEL_SQL_REGEX,
    SCRUBBED_ON_TERMINAL,
    TERMINAL_STATES,
    _sql_string_list,
)

_STATES_SQL = _sql_string_list(INBOX_STATES)
_TERMINAL_SQL = _sql_string_list(sorted(TERMINAL_STATES))
_ACTIVE_SQL = _sql_string_list(sorted(set(INBOX_STATES) - TERMINAL_STATES))
_ERROR_CLASSES_SQL = _sql_string_list(sorted(ERROR_CLASSES))
_ACTIONS_SQL = "'op','dn','lc','vc','ba'"

_TERMINAL_SCRUB_SQL = (
    f"CASE WHEN state IN ({_TERMINAL_SQL}) THEN "
    + " AND ".join(f"{column} IS NULL" for column in SCRUBBED_ON_TERMINAL)
    + " AND terminal_state_pending IS NULL"
    " ELSE true END"
)

_HANDLER_MARKER_ORDER_SQL = (
    # completion implies entry
    "(handler_completed_at IS NULL OR handler_entered_at IS NOT NULL)"
    " AND "
    # a recorded verdict implies both
    "(terminal_state_pending IS NULL OR ("
    "handler_entered_at IS NOT NULL AND handler_completed_at IS NOT NULL))"
    " AND "
    # a queued row has not run anything
    "CASE WHEN state IN ('pending','retry_wait') THEN "
    "handler_entered_at IS NULL AND handler_completed_at IS NULL "
    "AND terminal_state_pending IS NULL ELSE true END"
)

_RETRY_BUDGET_SQL = (
    # R25. ``retry_wait`` means "will be tried again", so a row parked there
    # with no attempts left is a contradiction: the sweep would either
    # dead-letter it on sight or -- worse -- not see it at all until the
    # backoff elapsed, all the while holding a live nonce and a chat id.
    #
    # No UNKNOWN loophole: ``attempt_count`` and ``max_attempts`` are both
    # NOT NULL, so the comparison is never SQL UNKNOWN and the CHECK cannot
    # be satisfied by absence. Other states are untouched -- a spent budget
    # is the normal shape of a terminal row.
    "CASE WHEN state = 'retry_wait' THEN attempt_count < max_attempts ELSE true END"
)


_RETRY_VOCABULARY_SQL = (
    # `retry_wait` has exactly one meaning, and it is not negotiable by a
    # caller: a failure that provably never reached the mutating region.
    # ``IS NOT DISTINCT FROM``, not ``=``: with a NULL ``error_class`` the
    # equality is SQL UNKNOWN, and a CHECK treats UNKNOWN as satisfied -- so
    # ``=`` would have let a retry with no error class through. Caught by
    # `test_the_database_refuses_any_other_retry_vocabulary`.
    "CASE WHEN state = 'retry_wait' THEN "
    "error_class IS NOT DISTINCT FROM 'pre_core_failure' "
    "AND outcome IS NULL ELSE true END"
    " AND "
    # A queued row has not failed at anything yet.
    "CASE WHEN state = 'pending' THEN "
    "error_class IS NULL AND outcome IS NULL ELSE true END"
)

_ACTIVE_RECONSTRUCTABLE_SQL = (
    f"CASE WHEN state IN ({_ACTIVE_SQL}) THEN "
    + " AND ".join(f"{column} IS NOT NULL" for column in ACTIVE_REQUIRED_COLUMNS)
    + " ELSE true END"
)


class TelegramCallbackInboxJob(Base):
    __tablename__ = "telegram_callback_inbox"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_telegram_callback_inbox_job_id"),
        UniqueConstraint(
            "update_digest", name="uq_telegram_callback_inbox_update_digest"
        ),
        CheckConstraint(f"state IN ({_STATES_SQL})", name="state"),
        CheckConstraint(
            f"terminal_state_pending IS NULL OR "
            f"terminal_state_pending IN ({_TERMINAL_SQL})",
            name="terminal_state_pending",
        ),
        CheckConstraint(
            f"attempt_count >= 0 AND attempt_count <= {MAX_ATTEMPTS}",
            name="attempt_count",
        ),
        CheckConstraint("max_attempts > 0", name="max_attempts"),
        CheckConstraint(f"action IS NULL OR action IN ({_ACTIONS_SQL})", name="action"),
        CheckConstraint(
            f"outcome IS NULL OR outcome ~ '{OUTCOME_LABEL_SQL_REGEX}'",
            name="outcome",
        ),
        CheckConstraint(
            f"error_class IS NULL OR error_class IN ({_ERROR_CLASSES_SQL})",
            name="error_class",
        ),
        # A claimed row with no ``started_at`` has no defined age, so the
        # recovery scan's staleness comparison is never true for it: the row
        # would sit in the state that says "a worker owns this" and be
        # invisible to the sweep forever.
        CheckConstraint(
            "CASE WHEN state = 'processing' THEN started_at IS NOT NULL ELSE true END",
            name="processing_started_at",
        ),
        # The three handler markers are causal facts, not independent flags.
        # Repair finalises a job *without re-running it* on the strength of a
        # recorded verdict, so a verdict that no entry ever produced must be
        # an impossible row, and a queued row must not remember a handler.
        CheckConstraint(_HANDLER_MARKER_ORDER_SQL, name="handler_marker_order"),
        CheckConstraint(_RETRY_VOCABULARY_SQL, name="retry_vocabulary"),
        CheckConstraint(_RETRY_BUDGET_SQL, name="retry_budget"),
        CheckConstraint(_TERMINAL_SCRUB_SQL, name="terminal_scrubbed"),
        CheckConstraint(_ACTIVE_RECONSTRUCTABLE_SQL, name="active_reconstructable"),
        Index("ix_telegram_callback_inbox_state_available", "state", "available_at"),
        {"schema": "review"},
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    # The only value that ever reaches Redis.
    job_id: Mapped[uuid.UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    # One-way delivery identity; the dedupe tombstone that outlives the scrub.
    update_digest: Mapped[str] = mapped_column(Text, nullable=False)

    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="pending")
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=str(MAX_ATTEMPTS)
    )

    received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    # Committed immediately BEFORE the callback core is invoked. This is the
    # only durable way to tell "died before entering the core" (safe to
    # re-run) from "died inside it" (ambiguous, never re-run).
    handler_entered_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    # Committed immediately AFTER it returns, together with the decided
    # terminal state, so a lost terminal commit is repaired rather than replayed.
    handler_completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True)
    )
    terminal_state_pending: Mapped[str | None] = mapped_column(Text)
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))

    # --- authority / PII: present only while the job is still runnable ---
    callback_query_id: Mapped[str | None] = mapped_column(Text)
    chat_id: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_user_id: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    subject_short: Mapped[str | None] = mapped_column(Text)
    dispatch_attempt_id: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True))
    membership_revision: Mapped[int | None] = mapped_column(Integer)
    membership_digest: Mapped[str | None] = mapped_column(Text)
    nonce: Mapped[str | None] = mapped_column(Text)

    # --- safe, survives the scrub ---
    outcome: Mapped[str | None] = mapped_column(Text)
    error_class: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


__all__ = ["TelegramCallbackInboxJob"]
