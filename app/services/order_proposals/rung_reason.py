"""Observation-only classification for rung ``void_reason`` text.

The classifier never changes a rung state and never authorizes a second send.  It
only maps known reason shapes to the closed vocabulary used by the additive
``void_reason_group`` column; anything else remains ``unclassified``.
"""

from __future__ import annotations

from app.models.rung_reason_vocabulary import (
    RUNG_VOID_REASON_BROKER_REJECTION,
    RUNG_VOID_REASON_CANCELLED_OR_EXPIRED,
    RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT,
    RUNG_VOID_REASON_POLICY_GUARD,
    RUNG_VOID_REASON_PROVIDER_THROTTLE,
    UNCLASSIFIED_VOID_REASON_GROUP,
    project_rung_void_reason_group,
)
from app.services.brokers.kis.order_throttle import (
    THROTTLE_MSG_CODES,
    is_provider_throttle_reject,
)

_DUPLICATE_PENDING_INTENT_MARKERS: tuple[str, ...] = (
    "duplicate order intent",
    "duplicate mock mirror intent",
    "conflicting order intent already reserved",
    "order intent already reserved",
    "pending order intent",
)

_BROKER_REJECTION_EXACT: tuple[str, ...] = (
    "broker rejected",
    "broker_rejected",
    "cancel_rejected",
    "provider rejected",
    "provider_rejected",
    "submit rejected",
    "submit_rejected",
)

_POLICY_GUARD_EXACT: tuple[str, ...] = (
    "insufficient balance",
    "insufficient_balance",
    "loss guard violation",
    "operator_denied",
    "telegram_deny",
    "loss_cut_preconditions_failed",
    "toss_auto_submission_frozen",
)

_POLICY_GUARD_MARKERS: tuple[str, ...] = (
    "authority=server_loss_guard_invalid",
    "guard_blocked:",
    "policy_guard:",
    "target_evidence_invalid:",
    "target_evidence_missing",
    "target_snapshot_mismatch:",
    "주문가능금액을 초과",
    "주문가능수량을 초과",
)

_CANCELLED_OR_EXPIRED_EXACT: tuple[str, ...] = (
    "cancelled",
    "canceled",
    "expired",
    "expiry",
    "order_expired",
)

_CANCELLED_OR_EXPIRED_PREFIXES: tuple[str, ...] = (
    "cancelled_",
    "canceled_",
    "expired_",
    "expiry_",
)


def _normalized_reason(reason: object) -> str:
    return " ".join(str(reason or "").strip().lower().split())


def _is_provider_throttle(reason: str) -> bool:
    """Adapt free text to the existing KIS throttle predicate.

    The decision itself belongs to ``is_provider_throttle_reject``.  The small
    code-token extraction only lets a persisted free-text reason retain the
    documented-code path (including ``EGW00201`` by itself); Korean message
    fallback remains owned by that predicate as well.
    """
    upper_reason = reason.upper()
    code = next(
        (candidate for candidate in THROTTLE_MSG_CODES if candidate in upper_reason),
        None,
    )
    return is_provider_throttle_reject(code, reason)


def classify_rung_void_reason(reason: object) -> str:
    """Classify known rung reason text without guessing at unknown text."""
    normalized = _normalized_reason(reason)

    # Reuse the provider-owned pure predicate; do not duplicate its code/text
    # semantics here.
    if _is_provider_throttle(normalized):
        return RUNG_VOID_REASON_PROVIDER_THROTTLE

    if any(marker in normalized for marker in _DUPLICATE_PENDING_INTENT_MARKERS):
        return RUNG_VOID_REASON_DUPLICATE_PENDING_INTENT

    if (
        normalized in _CANCELLED_OR_EXPIRED_EXACT
        or normalized.startswith(_CANCELLED_OR_EXPIRED_PREFIXES)
        or "authority=server_expired" in normalized
    ):
        return RUNG_VOID_REASON_CANCELLED_OR_EXPIRED

    if normalized in _POLICY_GUARD_EXACT or any(
        marker in normalized for marker in _POLICY_GUARD_MARKERS
    ):
        return RUNG_VOID_REASON_POLICY_GUARD

    if normalized in _BROKER_REJECTION_EXACT or normalized.startswith(
        ("broker_rejected:", "provider_rejected:", "submit_rejected:")
    ):
        return RUNG_VOID_REASON_BROKER_REJECTION

    return UNCLASSIFIED_VOID_REASON_GROUP


__all__ = [
    "classify_rung_void_reason",
    "project_rung_void_reason_group",
]
