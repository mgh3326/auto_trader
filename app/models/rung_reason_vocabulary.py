"""Closed vocabulary for rung ``void_reason`` observation groups.

This module deliberately has no ``app`` imports.  The ORM constraint, the
service-side validator, the classifier, and read-model projection all derive
their accepted group values from this tuple so the observation taxonomy cannot
drift between layers.
"""

from __future__ import annotations

RUNG_VOID_REASON_PROVIDER_THROTTLE = "provider_throttle"
RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT = "duplicate_pending_intent"
RUNG_VOID_REASON_BROKER_REJECTION = "broker_rejection"
RUNG_VOID_REASON_POLICY_GUARD = "policy_guard"
RUNG_VOID_REASON_CANCELLED_OR_EXPIRED = "cancelled_or_expired"
UNCLASSIFIED_VOID_REASON_GROUP = "unclassified"
RUNG_VOID_REASON_GROUPS: tuple[str, ...] = (
    RUNG_VOID_REASON_PROVIDER_THROTTLE,
    RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT,
    RUNG_VOID_REASON_BROKER_REJECTION,
    RUNG_VOID_REASON_POLICY_GUARD,
    RUNG_VOID_REASON_CANCELLED_OR_EXPIRED,
    UNCLASSIFIED_VOID_REASON_GROUP,
)

# A short alias keeps the vocabulary name usable for callers that do not need
# to repeat the rung qualifier.  It is the same tuple, not a second source.
VOID_REASON_GROUPS = RUNG_VOID_REASON_GROUPS


def sql_in_list(values: tuple[str, ...]) -> str:
    """Render a fixed vocabulary tuple as a SQL ``IN`` list."""
    return ", ".join(f"'{value}'" for value in values)


def is_rung_void_reason_group(value: object) -> bool:
    """Return whether ``value`` is an exact member of the closed vocabulary."""
    return type(value) is str and value in RUNG_VOID_REASON_GROUPS


def validate_rung_void_reason_group(value: object) -> str:
    """Validate a persisted group at the Python boundary.

    ``None`` is intentionally not accepted here; the ORM column remains
    nullable for pre-classification and historical rows, while a non-NULL
    value must be one of the closed groups.
    """
    if not is_rung_void_reason_group(value):
        raise ValueError(f"invalid rung void reason group: {value!r}")
    return value


def project_rung_void_reason_group(
    *, void_reason: object, stored_group: object
) -> str | None:
    """Project a row safely, preserving legacy NULLs as ``unclassified``.

    Rows with no reason have no group.  A row that does have a reason but was
    written before this additive column existed, or carries an invalid value
    from an untrusted read fixture, is honestly reported as ``unclassified``.
    """
    if void_reason is None:
        return None
    if is_rung_void_reason_group(stored_group):
        return stored_group
    return UNCLASSIFIED_VOID_REASON_GROUP


__all__ = [
    "RUNG_VOID_REASON_BROKER_REJECTION",
    "RUNG_VOID_REASON_CANCELLED_OR_EXPIRED",
    "RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT",
    "RUNG_VOID_REASON_GROUPS",
    "RUNG_VOID_REASON_POLICY_GUARD",
    "RUNG_VOID_REASON_PROVIDER_THROTTLE",
    "UNCLASSIFIED_VOID_REASON_GROUP",
    "VOID_REASON_GROUPS",
    "is_rung_void_reason_group",
    "project_rung_void_reason_group",
    "sql_in_list",
    "validate_rung_void_reason_group",
]
