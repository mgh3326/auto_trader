"""Pure types and validity checks for the order-proposal approval window."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum


class ApprovalWindowCode(StrEnum):
    ALLOW = "ALLOW"
    EXPIRED = "EXPIRED"
    INVALID_VALID_UNTIL = "INVALID_VALID_UNTIL"
    DEFER_SESSION_CLOSED = "DEFER_SESSION_CLOSED"
    CALENDAR_UNKNOWN = "CALENDAR_UNKNOWN"
    NO_EXECUTABLE_WINDOW = "NO_EXECUTABLE_WINDOW"


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def valid_until_block(
    value: object, *, now: datetime
) -> tuple[ApprovalWindowCode, str] | None:
    """Return a typed fail-closed validity error, or ``None`` while valid."""
    if not _aware(now):
        raise ValueError("approval-window now must be timezone-aware")
    if value is None:
        return ApprovalWindowCode.INVALID_VALID_UNTIL, "valid_until_missing"
    if not isinstance(value, datetime):
        return ApprovalWindowCode.INVALID_VALID_UNTIL, "valid_until_invalid_type"
    if not _aware(value):
        return ApprovalWindowCode.INVALID_VALID_UNTIL, "valid_until_naive"
    if now >= value:
        return ApprovalWindowCode.EXPIRED, "now_at_or_after_valid_until"
    return None
