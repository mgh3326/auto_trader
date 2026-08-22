"""Closed vocabularies for the durable Telegram callback inbox (W5).

Pure and dependency-free on purpose: the ORM model, the Alembic migration,
the worker and the recovery sweep all derive their string sets from here, so
the three layers cannot drift apart.
"""

from __future__ import annotations

import hashlib
import json
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
    "telegram_user_id",
    "action",
    "subject_short",
    "dispatch_attempt_id",
    "membership_revision",
    "membership_digest",
    "nonce",
)

MAX_ATTEMPTS = 3

TELEGRAM_USER_ID_MIN = 1
TELEGRAM_USER_ID_MAX = 2**52 - 1
TELEGRAM_UPDATE_ID_MIN = 1
TELEGRAM_UPDATE_ID_MAX = 2_147_483_647


def validate_telegram_user_id(value: object) -> int | None:
    """Return an exact bounded Telegram user id, without coercion."""
    if type(value) is not int:
        return None
    if not TELEGRAM_USER_ID_MIN <= value <= TELEGRAM_USER_ID_MAX:
        return None
    return value


def validate_telegram_update_id(value: object) -> int | None:
    """Return an exact bounded update id, without coercion."""
    if type(value) is not int:
        return None
    if not TELEGRAM_UPDATE_ID_MIN <= value <= TELEGRAM_UPDATE_ID_MAX:
        return None
    return value


def canonical_telegram_user_id_text(value: object) -> str | None:
    """Canonical decimal text for a validated exact Telegram user id."""
    user_id = validate_telegram_user_id(value)
    return None if user_id is None else str(user_id)


def parse_canonical_telegram_user_id_text(value: object) -> int | None:
    """Parse only canonical bounded decimal text retained by the inbox."""
    if type(value) is not str or not value or value[0] == "0":
        return None
    if not value.isascii() or not value.isdecimal():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if validate_telegram_user_id(parsed) != parsed:
        return None
    return parsed if str(parsed) == value else None


def is_malformed_attempt_budget(*, attempt_count: object, max_attempts: object) -> bool:
    """Whether an inbox row violates the fixed three-attempt protocol.

    This deliberately accepts only built-in ``int`` values.  ``bool`` is an
    ``int`` subclass in Python, but it is never an attempt count or budget;
    accepting it would make a malformed in-memory row look canonical before
    the worker reaches its database boundary.  No coercion is performed: a
    poisoned legacy value must be terminalised, not reinterpreted.
    """
    return (
        type(attempt_count) is not int
        or type(max_attempts) is not int
        or max_attempts != MAX_ATTEMPTS
        or attempt_count < 0
        or attempt_count > max_attempts
    )


def clamp_attempt_count(attempt_count: object) -> int:
    """Project a potentially malformed count onto the safe ``0..3`` range.

    This is used only while terminalising a malformed row and at the telemetry
    boundary.  It never grants execution authority: classification uses
    :func:`is_malformed_attempt_budget` first.
    """
    if type(attempt_count) is not int:
        return 0
    return min(max(attempt_count, 0), MAX_ATTEMPTS)


#: Deterministic backoff by attempt number (1-indexed). The recovery sweep
#: runs every minute, so these only need to be coarse.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (15, 60, 300)

#: How old a ``processing`` row must be before the recovery scan will even
#: look at it. This is a SCAN FILTER, never an authority: the advisory lock
#: decides whether a live worker still owns the job. A row that is "stale" but
#: whose lock is held is skipped, every time.
PROCESSING_STALE_AFTER_SECONDS = 300

RECOVERY_SCAN_LIMIT = 20

#: How many candidates one sweep may *look at* per job it may actually run.
#:
#: R29. These were the same number, so a candidate whose advisory lock was
#: held consumed the tick's whole budget and the queued work behind it was
#: never selected. Looking at a locked row is cheap -- one
#: ``pg_try_advisory_lock`` that fails -- while running one is not, so the two
#: are now separate limits: contention costs a scan slot, execution costs an
#: execution slot.
RECOVERY_SCAN_OVERFETCH = 5

#: An absolute ceiling on how many candidate rows one sweep may fetch,
#: regardless of the execution cap, so no caller can ask for an arbitrarily
#: large result set.
#:
#: A cap on the result, not on the read. ``EXPLAIN`` on a tier query shows the
#: predicate using ``(state, available_at)`` and the ordering still sorting the
#: eligible set as a bounded-memory top-N, because no index matches
#: ``(received_at, job_id)``. What this bounds is rows returned and sort
#: memory; bounding the physical read would need an index built for these
#: predicates and this ordering.
RECOVERY_SCAN_HARD_CAP = 1_000


def recovery_scan_cap(limit: int) -> int:
    """How many candidate ids one sweep may fetch for an execution cap."""
    return min(max(limit, 1) * RECOVERY_SCAN_OVERFETCH, RECOVERY_SCAN_HARD_CAP)


#: The canonical four-tier recovery ring. R34 keeps malformed active rows in
#: their own due-independent tier; they are not folded into exhausted because
#: an invalid count/budget pair must be normalised in the same terminal write
#: that scrubs authority, whether it is pending, processing, or a future
#: retry. The database cursor rotates *between* sweeps through this exact
#: order; it does not alter the predicates or grant row authority.
TIER_MALFORMED = 0
TIER_EXHAUSTED = 1
TIER_QUEUED = 2
TIER_STALE = 3
RECOVERY_TIER_RING: tuple[int, ...] = (
    TIER_MALFORMED,
    TIER_EXHAUSTED,
    TIER_QUEUED,
    TIER_STALE,
)

#: The share of one scan each non-queued tier is guaranteed.
#:
#: R29. A single global ordering cannot be fair in both directions: whichever
#: tier sorts first starves the other as soon as it is bigger than one scan.
#: Queued work first starved stale recovery; stale work first was the original
#: bug. R34 adds a malformed scrub tier, but it too gets only a reserved
#: slice. The queued tier takes what is left, which is most of it -- reserving
#: a little for tiers that would otherwise never be reached costs the common
#: case almost nothing.
RECOVERY_TIER_RESERVE_DIVISOR = 5


def recovery_tier_quotas(limit: int) -> dict[int, int]:
    """How many candidates of each tier one sweep may look at."""
    cap = recovery_scan_cap(limit)
    reserve = max(1, cap // RECOVERY_TIER_RESERVE_DIVISOR)
    return {
        TIER_MALFORMED: reserve,
        TIER_EXHAUSTED: reserve,
        TIER_STALE: reserve,
        TIER_QUEUED: max(1, cap - 3 * reserve),
    }


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
    #: A legacy row's count/budget pair is not the fixed protocol shape.
    #: It is terminalised before any repair, exhaustion, due-time, or handler
    #: path can use its authority.
    ATTEMPT_BUDGET_INVALID = "attempt_budget_invalid"
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
# Outcome categories
# --------------------------------------------------------------------------

UNCLASSIFIED_OUTCOME = "unclassified"

#: The only labels allowed to survive a terminal scrub. This is deliberately a
#: closed display vocabulary, not a shape rule: a syntactically-valid callback
#: reason can carry authority material just as readily as an invalid one.
#: Keep this inventory aligned with the callback-core reason audit.
OUTCOME_CATEGORIES: tuple[str, ...] = (
    # decisions / successful completion
    "approved",
    "approved_with_window_block",
    "denied",
    "needs_reconfirm",
    "batch_approved",
    "auto_veto_cancelled",
    "auto_veto_filled",
    "auto_veto_failed",
    "auto_veto_unconfirmed",
    "loss_cut_confirmation_required",
    # window / callback core
    "expired",
    "invalid_valid_until",
    "defer_session_closed",
    "calendar_unknown",
    "no_executable_window",
    "approval_window_blocked",
    "proposal_not_found",
    "chat_not_allowed",
    "lease_held",
    "nonce_mismatch",
    "nonce_replay",
    "internal_error",
    # loss-cut confirmation
    "loss_cut_confirmation_missing",
    "loss_cut_confirmation_invalid",
    "loss_cut_confirmation_expired",
    "loss_cut_confirmation_principal_mismatch",
    "loss_cut_confirmation_binding_mismatch",
    "loss_cut_confirmation_dispatch_failed",
    # published binding / dispatch
    "approval_callback_subject_mismatch",
    "approval_dispatch_state_invalid",
    "approval_dispatch_card_kind_invalid",
    "approval_dispatch_pending",
    "approval_dispatch_sent_superseded",
    "approval_dispatch_failed",
    "approval_dispatch_partial_failed",
    "approval_dispatch_failed_superseded",
    "approval_dispatch_attempt_mismatch",
    "approval_membership_revision_mismatch",
    "approval_membership_digest_mismatch",
    "approval_card_action_mismatch",
    "auto_veto_not_available",
    "auto_veto_nonce_requires_vc",
    # batch
    "batch_window_blocked",
    "approval_batch_not_found",
    "approval_batch_too_small",
    "approval_batch_expired",
    "approval_batch_chat_mismatch",
    "approval_batch_nonce_mismatch",
    "approval_batch_nonce_replay",
    "approval_batch_member_snapshot_invalid",
    "approval_batch_membership_changed",
    "approval_batch_membership_digest_mismatch",
    # Explicit projection families, followed by the safe fallback.
    "proposal_superseded_by",
    "proposal_terminal",
    "approval_window",
    "approval_batch_member_stale",
    UNCLASSIFIED_OUTCOME,
)

#: Raw ``prefix:payload`` families that preserve only their fixed prefix. The
#: suffix never reaches a terminal row, log record, or Sentry field.
PAYLOAD_OUTCOME_CATEGORIES: tuple[str, ...] = (
    "proposal_superseded_by",
    "proposal_terminal",
    "approval_window",
    "approval_batch_member_stale",
)

_OUTCOME_CATEGORY_SET = frozenset(OUTCOME_CATEGORIES)
_PAYLOAD_OUTCOME_CATEGORY_SET = frozenset(PAYLOAD_OUTCOME_CATEGORIES)


def normalize_outcome(reason: object) -> str | None:
    """Project a raw callback-core reason onto one closed safe category.

    ``classify_verdict`` first derives state and retry authority from the raw
    typed reason. This post-classification boundary then keeps only a display
    category. Four audited ``prefix:payload`` families project to their fixed
    prefix; every other value, including an otherwise-valid lowercase slug,
    becomes ``unclassified``.

    Only an exact built-in ``str`` or a genuine ``StrEnum`` is accepted.
    Arbitrary objects and ``str`` subclasses are never coerced or dispatched:
    their ``str``/``repr`` or overridden string methods could itself carry
    authority material.
    """
    if reason is None:
        return None
    if type(reason) is str:
        raw_reason = reason
    elif isinstance(reason, StrEnum):
        raw_reason = reason.value
    else:
        return UNCLASSIFIED_OUTCOME
    if type(raw_reason) is not str:
        return UNCLASSIFIED_OUTCOME
    label = str.lower(str.strip(raw_reason))
    if label in _OUTCOME_CATEGORY_SET:
        return label
    category, separator, _payload = label.partition(":")
    if separator and category in _PAYLOAD_OUTCOME_CATEGORY_SET:
        return category
    return UNCLASSIFIED_OUTCOME


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
    normalized_update_id = (
        None if update_id is None else validate_telegram_update_id(update_id)
    )
    if update_id is not None and normalized_update_id is None:
        raise ValueError("invalid_telegram_identifier")
    normalized_query = None if callback_query_id is None else str(callback_query_id)
    normalized_update = (
        None if normalized_update_id is None else str(normalized_update_id)
    )
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
    normalized_update_id = validate_telegram_update_id(update_id)
    if normalized_update_id is None:
        raise ValueError("invalid_telegram_identifier")
    canonical = json.dumps(
        {"domain": UPDATE_IDENTITY_DOMAIN, "update_id": str(normalized_update_id)},
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
    "clamp_attempt_count",
    "ERROR_CLASSES",
    "INBOX_STATES",
    "MAX_ATTEMPTS",
    "IGNORED_HANDLER_RETRY_KEYS",
    "OUTCOME_CATEGORIES",
    "PAYLOAD_OUTCOME_CATEGORIES",
    "PROCESSING_STALE_AFTER_SECONDS",
    "RECOVERY_CLAIMABLE_STATES",
    "TIER_EXHAUSTED",
    "TIER_MALFORMED",
    "TIER_QUEUED",
    "TIER_STALE",
    "RECOVERY_TIER_RESERVE_DIVISOR",
    "RECOVERY_SCAN_HARD_CAP",
    "RECOVERY_SCAN_LIMIT",
    "RECOVERY_SCAN_OVERFETCH",
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
    "is_malformed_attempt_budget",
    "job_advisory_lock_key",
    "recovery_scan_cap",
    "recovery_tier_quotas",
    "normalize_outcome",
]
