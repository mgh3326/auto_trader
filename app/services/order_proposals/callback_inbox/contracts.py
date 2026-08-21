"""Closed vocabularies for the durable Telegram callback inbox (W5).

Pure and dependency-free on purpose: the ORM model, the Alembic migration,
the worker and the recovery sweep all derive their string sets from here, so
the three layers cannot drift apart.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from enum import StrEnum

# --------------------------------------------------------------------------
# Job states
# --------------------------------------------------------------------------


class InboxState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    DISCARDED = "discarded"
    RETRY_WAIT = "retry_wait"
    DEAD_LETTER = "dead_letter"


INBOX_STATES: tuple[str, ...] = tuple(sorted(state.value for state in InboxState))

#: Terminal states. Reaching one scrubs every authority/PII column.
TERMINAL_STATES: frozenset[str] = frozenset(
    {
        InboxState.SUCCEEDED.value,
        InboxState.DISCARDED.value,
        InboxState.DEAD_LETTER.value,
    }
)

#: Claimable by the per-job worker task. Never ``processing``: only recovery
#: may reclaim a row another process might still own, and only after taking
#: the advisory lock.
WORKER_CLAIMABLE_STATES: frozenset[str] = frozenset(
    {InboxState.PENDING.value, InboxState.RETRY_WAIT.value}
)

#: Claimable by the recovery sweep.
RECOVERY_CLAIMABLE_STATES: frozenset[str] = WORKER_CLAIMABLE_STATES | {
    InboxState.PROCESSING.value
}

#: NULLed the instant a job goes terminal. Enforced by a DB CHECK, not only by
#: the service that is supposed to do it.
SCRUBBED_ON_TERMINAL: tuple[str, ...] = (
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
    # R28. A one-way digest of the Telegram ``update_id``, kept only to tell
    # a genuine redelivery from a different call reusing the same callback
    # query id. It is verification material, not identity, so it goes when
    # the rest of the authority does.
    "update_identity_digest",
)

#: Must be explicitly non-NULL while a job is still runnable, or the worker
#: could not rebuild and re-gate the envelope. ``callback_query_id`` and
#: ``message_id`` are deliberately absent: both are optional in the legacy
#: inline handler (it no-ops the answer/edit when they are missing), so
#: requiring them would reject updates today's code accepts.
ACTIVE_REQUIRED_COLUMNS: tuple[str, ...] = (
    "chat_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)

MAX_ATTEMPTS = 3

#: Deterministic backoff by attempt number (1-indexed). The recovery sweep
#: runs every minute, so these only need to be coarse.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (15, 60, 300)

#: How old a ``processing`` row must be before the recovery scan will even
#: look at it. This is a SCAN FILTER, never an authority: the advisory lock
#: decides whether a live worker still owns the job. A row that is "stale" but
#: whose lock is held is skipped, every time.
PROCESSING_STALE_AFTER_SECONDS = 300

RECOVERY_SCAN_LIMIT = 20


# --------------------------------------------------------------------------
# Retry algebra
# --------------------------------------------------------------------------

#: Keys a handler might return hoping to buy a replay. **None of them is
#: honoured.** Retry authority is worker-owned: it exists only as a
#: :class:`~app.services.order_proposals.callback_inbox.worker.PreCoreFailure`
#: raised from the phase that runs *before* the core is entered, and the
#: service refuses to act on it unless the durable row still proves
#: pre-entry. A handler that has already mutated could return any of these
#: just as easily as one that has not, which is exactly why a returned value
#: can never be the proof.
IGNORED_HANDLER_RETRY_KEYS: frozenset[str] = frozenset(
    {"mutation_not_started", "retry", "retryable", "safe_to_retry"}
)

#: EMPTY, and that is the point. The callback core funnels every exception
#: into ``{"handled": False, "reason": "internal_error"}``, including one
#: raised after ``revalidate_and_submit`` reached the broker and before the
#: transaction committed. That rollback leaves the nonce unconsumed and the
#: published binding valid, so a reason-string-driven retry would look legal
#: and submit a second time. No reason string is evidence either.
RETRYABLE_HANDLER_REASONS: frozenset[str] = frozenset()


class ErrorClass(StrEnum):
    #: The mutating region was provably not entered: a worker-owned
    #: ``PreCoreFailure`` raised above the ``handler_entered_at`` commit, and
    #: re-confirmed by ``schedule_retry``'s conditional UPDATE against the
    #: durable markers. The only re-runnable class. Nothing a handler returns
    #: can reach it -- see :data:`IGNORED_HANDLER_RETRY_KEYS`.
    PRE_CORE_FAILURE = "pre_core_failure"
    #: The core was entered and did not report a verdict. Unsafe to replay.
    HANDLER_AMBIGUOUS = "handler_ambiguous"
    #: The core broke its never-raise contract. Also unsafe to replay.
    HANDLER_EXCEPTION = "handler_exception"
    #: The chat left the allowlist while the job was queued.
    CHAT_REVOKED = "chat_revoked"
    #: The stored envelope no longer rebuilds into a valid callback.
    ENVELOPE_INVALID = "envelope_invalid"
    #: ``MAX_ATTEMPTS`` re-runnable attempts spent without a verdict.
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"


ERROR_CLASSES: frozenset[str] = frozenset(item.value for item in ErrorClass)
RETRYABLE_ERROR_CLASSES: frozenset[str] = frozenset({ErrorClass.PRE_CORE_FAILURE.value})


class WorkerStatus(StrEnum):
    DISABLED = "disabled"
    NOT_FOUND = "not_found"
    NOT_CLAIMABLE = "not_claimable"
    LOCK_CONTENDED = "lock_contended"
    SUCCEEDED = "succeeded"
    DISCARDED = "discarded"
    RETRY_SCHEDULED = "retry_scheduled"
    DEAD_LETTER = "dead_letter"


WORKER_STATUSES: frozenset[str] = frozenset(item.value for item in WorkerStatus)

#: Terminal state -> the status the task reports for it.
TERMINAL_STATE_STATUS: dict[str, str] = {
    InboxState.SUCCEEDED.value: WorkerStatus.SUCCEEDED.value,
    InboxState.DISCARDED.value: WorkerStatus.DISCARDED.value,
    InboxState.DEAD_LETTER.value: WorkerStatus.DEAD_LETTER.value,
}


# --------------------------------------------------------------------------
# Outcome labels
# --------------------------------------------------------------------------

OUTCOME_LABEL_SQL_REGEX = "^[a-z0-9_]{1,64}$"
OUTCOME_LABEL_PATTERN = re.compile(r"[a-z0-9_]{1,64}")
UNCLASSIFIED_OUTCOME = "unclassified"


def normalize_outcome(reason: object) -> str | None:
    """Reduce a callback-core reason to a storable slug.

    The core's reasons are stable identifiers, but some carry a payload
    (``proposal_superseded_by:<uuid>``, ``approval_window:EXPIRED:<detail>``).
    Only the leading identifier is kept, so a terminal row can never hold
    anything reconstructable, and anything that is not a clean slug degrades
    to ``unclassified`` rather than being stored verbatim.
    """
    if reason is None:
        return None
    label = str(reason).split(":", 1)[0].strip().lower()
    if not OUTCOME_LABEL_PATTERN.fullmatch(label):
        return UNCLASSIFIED_OUTCOME
    return label


# --------------------------------------------------------------------------
# Delivery identity
# --------------------------------------------------------------------------

#: Domain separation: a digest from this surface can never collide with, or be
#: replayed as, a digest built anywhere else in the system.
UPDATE_DIGEST_DOMAIN = "order_proposals.telegram_callback_inbox.delivery.v1"


class DeliveryIdentityMissing(ValueError):
    """Neither ``update_id`` nor a callback-query id was present."""


UPDATE_IDENTITY_DOMAIN = "order_proposals.telegram_callback_inbox.update_id.v1"


def build_update_digest(*, update_id: object, callback_query_id: object) -> str:
    """Return the one-way delivery identity digest used for dedupe.

    The identity is the **callback query id alone** when Telegram supplies
    one. It hashed the *pair* until R28, which meant the same callback query
    id arriving under a different ``update_id`` produced a different digest,
    passed the unique index as an unrelated delivery, and was accepted,
    persisted and queued a second time -- with whatever binding it happened
    to carry. One click is one delivery no matter how it is re-wrapped, so
    ``update_id`` no longer participates in the identity at all. It moves to
    the verification projection instead (``build_update_identity_digest``),
    where a mismatch is a conflict rather than a second row.

    ``update_id`` remains a supported *fallback* identity for an update that
    carries no callback query at all. The two kinds are domain-separated, so
    an update id of ``7`` and a callback query id of ``"7"`` cannot collide.

    Built only from identifiers Telegram guarantees are unique per bot --
    never from the raw payload -- and canonicalised so the digest depends on
    the identity rather than on JSON key order.
    """
    normalized_query = None if callback_query_id is None else str(callback_query_id)
    normalized_update = None if update_id is None else str(update_id)
    if normalized_query is not None:
        kind, value = "callback_query_id", normalized_query
    elif normalized_update is not None:
        kind, value = "update_id", normalized_update
    else:
        raise DeliveryIdentityMissing("no delivery identity on the update")
    canonical = json.dumps(
        {"domain": UPDATE_DIGEST_DOMAIN, "identity_kind": kind, "value": value},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_update_identity_digest(*, update_id: object) -> str | None:
    """One-way digest of the Telegram ``update_id``, for tamper detection.

    Stored rather than the raw value: the row only ever needs to answer "is
    this the same update as before?", and a digest answers that without
    retaining a Telegram sequence number past the terminal scrub.
    """
    if update_id is None:
        return None
    canonical = json.dumps(
        {"domain": UPDATE_IDENTITY_DOMAIN, "update_id": str(update_id)},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Advisory lock key
# --------------------------------------------------------------------------

ADVISORY_LOCK_DOMAIN = "order_proposals.telegram_callback_inbox.job.v1"


def job_advisory_lock_key(job_id: uuid.UUID) -> int:
    """Stable signed bigint accepted by PostgreSQL advisory-lock functions."""
    digest = hashlib.sha256(f"{ADVISORY_LOCK_DOMAIN}|{job_id}".encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


def _sql_string_list(values: object) -> str:
    """Render a vocabulary as a SQL ``IN`` list, for CHECK constraints."""
    return ",".join(f"'{value}'" for value in values)  # type: ignore[union-attr]


__all__ = [
    "ACTIVE_REQUIRED_COLUMNS",
    "ADVISORY_LOCK_DOMAIN",
    "ERROR_CLASSES",
    "INBOX_STATES",
    "MAX_ATTEMPTS",
    "IGNORED_HANDLER_RETRY_KEYS",
    "OUTCOME_LABEL_PATTERN",
    "OUTCOME_LABEL_SQL_REGEX",
    "PROCESSING_STALE_AFTER_SECONDS",
    "RECOVERY_CLAIMABLE_STATES",
    "RECOVERY_SCAN_LIMIT",
    "RETRYABLE_ERROR_CLASSES",
    "RETRYABLE_HANDLER_REASONS",
    "RETRY_BACKOFF_SECONDS",
    "SCRUBBED_ON_TERMINAL",
    "TERMINAL_STATES",
    "TERMINAL_STATE_STATUS",
    "UNCLASSIFIED_OUTCOME",
    "UPDATE_DIGEST_DOMAIN",
    "WORKER_CLAIMABLE_STATES",
    "WORKER_STATUSES",
    "DeliveryIdentityMissing",
    "ErrorClass",
    "InboxState",
    "WorkerStatus",
    "build_update_digest",
    "job_advisory_lock_key",
    "normalize_outcome",
]
