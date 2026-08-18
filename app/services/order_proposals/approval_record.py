"""Typed, decision-free contract for approval audit records (ROB-1255).

This module deliberately has no database, broker, or approval-decision imports.
The values describe facts that have already occurred; they never authorize an
approval or change nonce validity.
"""

from __future__ import annotations

import hashlib
from enum import StrEnum


class ApprovalRecordEventType(StrEnum):
    FIRST_STAGE_APPROVED = "first_stage_approved"
    SECOND_STAGE_DISPATCHED = "second_stage_dispatched"
    SECOND_STAGE_CLICKED = "second_stage_clicked"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"


class ApprovalRecordTimingSource(StrEnum):
    TELEGRAM_CALLBACK_RECEIVED = "telegram_callback_received"
    TELEGRAM_DISPATCH_STARTED = "telegram_dispatch_started"
    APPROVAL_DEADLINE = "approval_deadline"
    PROPOSAL_DEADLINE = "proposal_deadline"
    SUPERSEDE_TRANSACTION = "supersede_transaction"


APPROVAL_RECORD_EVENT_TYPES = frozenset(item.value for item in ApprovalRecordEventType)
APPROVAL_RECORD_TIMING_SOURCES = frozenset(
    item.value for item in ApprovalRecordTimingSource
)
APPROVAL_RECORD_ACTOR_KINDS = frozenset({"telegram_user", "web_user", "system"})
APPROVAL_RECORD_CHANNELS = frozenset({"telegram", "web", "system"})


def approval_nonce_digest(nonce: str | None) -> str | None:
    """Return a one-way nonce fingerprint; raw approval nonces are never audited."""
    if nonce is None:
        return None
    return hashlib.sha256(nonce.encode("utf-8")).hexdigest()


__all__ = [
    "APPROVAL_RECORD_ACTOR_KINDS",
    "APPROVAL_RECORD_CHANNELS",
    "APPROVAL_RECORD_EVENT_TYPES",
    "APPROVAL_RECORD_TIMING_SOURCES",
    "ApprovalRecordEventType",
    "ApprovalRecordTimingSource",
    "approval_nonce_digest",
]
