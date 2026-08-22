"""Closed, coercion-free TaskIQ result projections for the callback inbox.

The task result backend is a separate trust boundary from the durable inbox.
Only this module turns untrusted worker/recovery return values into the small,
fixed result shapes that may be serialized by TaskIQ.  It deliberately has no
database, settings, gate, or worker imports: callers supply already-approved
authoritative values and this module either makes a fresh safe copy or rejects
the value without rendering it.
"""

from __future__ import annotations

import math
import uuid

PROCESS_STATUS_ORDER: tuple[str, ...] = (
    "dead_letter",
    "discarded",
    "lock_contended",
    "not_claimable",
    "not_found",
    "retry_scheduled",
    "succeeded",
)
PROCESS_STATUSES: frozenset[str] = frozenset(PROCESS_STATUS_ORDER)

RECOVERY_ITEM_STATUS_ORDER: tuple[str, ...] = (*PROCESS_STATUS_ORDER, "error")
RECOVERY_ITEM_STATUSES: frozenset[str] = frozenset(RECOVERY_ITEM_STATUS_ORDER)
NON_CLAIMED_RECOVERY_ITEM_STATUSES: frozenset[str] = frozenset(
    {"lock_contended", "not_claimable", "not_found"}
)

RECOVERY_REPORT_KEYS: frozenset[str] = frozenset(
    {"status", "scanned", "claimed", "statuses", "backlog"}
)
RECOVERY_BACKLOG_KEYS: frozenset[str] = frozenset(
    {
        "pending",
        "processing",
        "retry_wait",
        "dead_letter",
        "oldest_pending_age_seconds",
    }
)
RECOVERY_BACKLOG_COUNT_KEYS: tuple[str, ...] = (
    "pending",
    "processing",
    "retry_wait",
    "dead_letter",
)

_MISSING = object()


def canonical_job_id(value: object) -> str | None:
    """Return only an exact canonical UUID wire value.

    The ``type`` check is intentionally before parsing: a string subclass can
    override operations used by UUID parsing, and neither UUID objects nor
    arbitrary pickled TaskIQ values are part of the Redis wire contract.
    """
    if type(value) is not str:
        return None
    try:
        parsed = uuid.UUID(value)
    except (AttributeError, TypeError, ValueError):
        return None
    if str(parsed) != value:
        return None
    return value


def invalid_job_id_result() -> dict[str, str]:
    """The non-reflecting result for an invalid TaskIQ argument."""
    return {"status": "invalid_job_id"}


def disabled_job_result(job_id: str) -> dict[str, str]:
    """Build the fixed result for a canonical job while the worker gate is off."""
    return {"status": "disabled", "job_id": job_id}


def error_job_result(job_id: str) -> dict[str, str]:
    """Build the fixed result for an ordinary failure or malformed worker output."""
    return {"status": "error", "job_id": job_id}


def project_job_result(value: object, *, job_id: str) -> dict[str, str] | None:
    """Make a safe copy of one worker result, or reject it without coercion.

    Extra fields are expressly untrusted and ignored.  The two required fields
    are found by iterating the exact built-in dict rather than ``get`` so a
    string-subclass key cannot impersonate a required field.
    """
    fields = _required_worker_fields(value)
    if fields is None:
        return None

    status, returned_job_id = fields
    if type(status) is not str or status not in PROCESS_STATUSES:
        return None
    if type(returned_job_id) is not str or returned_job_id != job_id:
        return None
    return {"status": status, "job_id": job_id}


def recovery_error_result() -> dict[str, str]:
    """The fixed result for an ordinary recovery failure or invalid report."""
    return {"status": "error"}


def empty_recovery_statuses() -> dict[str, int]:
    """Return the complete, zero-filled recovery status vocabulary."""
    return dict.fromkeys(RECOVERY_ITEM_STATUS_ORDER, 0)


def project_recovery_report(
    value: object,
    *,
    execution_limit: int,
    scan_cap: int,
) -> dict[str, object] | None:
    """Make a fresh closed copy of a recovery aggregate report.

    This validates every allowed field before copying it.  Unknown fields and
    all non-exact built-in container/scalar types are rejected without
    rendering their values.
    """
    if not _is_nonnegative_int(execution_limit) or not _is_nonnegative_int(scan_cap):
        return None

    report = _exact_fields(value, expected_keys=RECOVERY_REPORT_KEYS)
    if report is None:
        return None

    status = report["status"]
    scanned = report["scanned"]
    claimed = report["claimed"]
    if type(status) is not str or status != "ok":
        return None
    if not _is_nonnegative_int(scanned) or not _is_nonnegative_int(claimed):
        return None
    if claimed > execution_limit or claimed > scanned or scanned > scan_cap:
        return None

    statuses = _recovery_statuses(report["statuses"])
    if statuses is None or sum(statuses.values()) != scanned:
        return None
    expected_claimed = sum(
        count
        for item_status, count in statuses.items()
        if item_status not in NON_CLAIMED_RECOVERY_ITEM_STATUSES
    )
    if claimed != expected_claimed:
        return None

    backlog = _recovery_backlog(report["backlog"])
    if backlog is None:
        return None

    return {
        "status": "ok",
        "scanned": scanned,
        "claimed": claimed,
        "statuses": statuses,
        "backlog": backlog,
    }


def recovery_item_status(value: object, *, job_id: str) -> str | None:
    """Validate a worker result for recovery without copying untrusted fields."""
    result = project_job_result(value, job_id=job_id)
    if result is None:
        return None
    return result["status"]


def _required_worker_fields(value: object) -> tuple[object, object] | None:
    """Find required exact-string keys while ignoring all other worker fields."""
    if type(value) is not dict:
        return None

    status: object = _MISSING
    job_id: object = _MISSING
    for key, item in value.items():
        # Do not compare or render an untrusted extra key.
        if type(key) is not str:
            continue
        if key == "status":
            status = item
        elif key == "job_id":
            job_id = item
    if status is _MISSING or job_id is _MISSING:
        return None
    return status, job_id


def _exact_fields(
    value: object, *, expected_keys: frozenset[str]
) -> dict[str, object] | None:
    """Return allowed exact-string fields, rejecting unknown/missing fields."""
    if type(value) is not dict:
        return None

    fields: dict[str, object] = {}
    for key, item in value.items():
        # Type check before set membership: hostile keys must not get equality
        # or hash callbacks at this boundary.
        if type(key) is not str or key not in expected_keys:
            return None
        fields[key] = item
    if len(fields) != len(expected_keys) or frozenset(fields) != expected_keys:
        return None
    return fields


def _recovery_statuses(value: object) -> dict[str, int] | None:
    fields = _exact_fields(value, expected_keys=RECOVERY_ITEM_STATUSES)
    if fields is None:
        return None

    statuses: dict[str, int] = {}
    for status in RECOVERY_ITEM_STATUS_ORDER:
        count = fields[status]
        if not _is_nonnegative_int(count):
            return None
        statuses[status] = count
    return statuses


def _recovery_backlog(value: object) -> dict[str, int | float | None] | None:
    fields = _exact_fields(value, expected_keys=RECOVERY_BACKLOG_KEYS)
    if fields is None:
        return None

    backlog: dict[str, int | float | None] = {}
    for key in RECOVERY_BACKLOG_COUNT_KEYS:
        count = fields[key]
        if not _is_nonnegative_int(count):
            return None
        backlog[key] = count

    age = fields["oldest_pending_age_seconds"]
    if age is not None and (
        type(age) is not float or age < 0.0 or not math.isfinite(age)
    ):
        return None
    backlog["oldest_pending_age_seconds"] = age
    return backlog


def _is_nonnegative_int(value: object) -> bool:
    return type(value) is int and value >= 0


__all__ = [
    "NON_CLAIMED_RECOVERY_ITEM_STATUSES",
    "PROCESS_STATUSES",
    "RECOVERY_ITEM_STATUSES",
    "canonical_job_id",
    "disabled_job_result",
    "empty_recovery_statuses",
    "error_job_result",
    "invalid_job_id_result",
    "project_job_result",
    "project_recovery_report",
    "recovery_error_result",
    "recovery_item_status",
]
