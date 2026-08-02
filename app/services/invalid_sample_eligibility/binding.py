"""ROB-1036 — immutable ``invalid_sample_cleanup`` binding contract.

A cleanup leg is only auditable if the purpose, the sample it is cleaning up,
the approval it was authorised by, the operational mission it belongs to, and
the broker lifecycle it produced are bound together at the moment of authoring
and never edited afterwards.

The binding is authored once, in the approval window, for one session.  An
expired or missed window fails closed, and a binding is never carried over into
a later session — a new session needs a new approval, not a reused one.

This module is pure: stdlib only, no DB, broker, network, or clock access.  The
caller supplies the clock, exactly like ``verify_packet_freshness``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from app.services.invalid_sample_eligibility.contract import (
    CONTRACT_VERSION,
    canonical_evidence_hash,
)

#: The only purpose this binding may carry.  A different cleanup purpose needs
#: its own contract, not a widened enum member.
CLEANUP_PURPOSE = "invalid_sample_cleanup"


class CleanupBindingError(ValueError):
    """Raised when a binding cannot be authored under the contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class CleanupBinding:
    """Frozen purpose ↔ sample ↔ approval ↔ mission ↔ lifecycle binding."""

    purpose: str
    contract_version: str
    forecast_id: uuid.UUID
    sample_ref: str
    approval_id: str
    approval_hash: str
    approval_expires_at: datetime
    approval_session_id: str
    mission_id: str
    account_mode: str
    client_order_id: str
    lifecycle_correlation_id: str

    def __post_init__(self) -> None:
        if self.purpose != CLEANUP_PURPOSE:
            raise CleanupBindingError(
                "unsupported_purpose",
                f"purpose must be {CLEANUP_PURPOSE!r}; got {self.purpose!r}",
            )
        if not isinstance(self.forecast_id, uuid.UUID):
            raise TypeError("forecast_id must be a uuid.UUID")
        if self.approval_expires_at.tzinfo is None or (
            self.approval_expires_at.tzinfo.utcoffset(self.approval_expires_at) is None
        ):
            raise CleanupBindingError(
                "naive_approval_expires_at",
                "approval_expires_at must be timezone-aware",
            )
        for field_name in (
            "contract_version",
            "sample_ref",
            "approval_id",
            "approval_hash",
            "approval_session_id",
            "mission_id",
            "account_mode",
            "client_order_id",
            "lifecycle_correlation_id",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise CleanupBindingError(
                    "missing_binding_field", f"{field_name} must be a non-empty string"
                )
            object.__setattr__(self, field_name, value.strip())

    @property
    def binding_hash(self) -> str:
        """Canonical digest over every bound identity."""

        return canonical_evidence_hash(
            {
                "purpose": self.purpose,
                "contract_version": self.contract_version,
                "forecast_id": str(self.forecast_id),
                "sample_ref": self.sample_ref,
                "approval_id": self.approval_id,
                "approval_hash": self.approval_hash,
                "approval_session_id": self.approval_session_id,
                "mission_id": self.mission_id,
                "account_mode": self.account_mode,
                "client_order_id": self.client_order_id,
                "lifecycle_correlation_id": self.lifecycle_correlation_id,
            }
        )


def build_cleanup_binding(
    *,
    forecast_id: uuid.UUID,
    sample_ref: str,
    approval_id: str,
    approval_hash: str,
    approval_expires_at: datetime,
    approval_session_id: str,
    mission_id: str,
    account_mode: str,
    client_order_id: str,
    lifecycle_correlation_id: str,
    now: datetime,
    session_id: str,
    contract_version: str = CONTRACT_VERSION,
) -> CleanupBinding:
    """Author a binding, refusing an expired approval or a cross-session reuse.

    Raises:
        CleanupBindingError('naive_now') if ``now`` has no tzinfo.
        CleanupBindingError('approval_window_expired') if the approval window
            has closed — a missed window is not silently carried forward.
        CleanupBindingError('cross_session_carry_over_blocked') if the approval
            was issued to a different session.
    """

    if now.tzinfo is None or now.tzinfo.utcoffset(now) is None:
        raise CleanupBindingError(
            "naive_now", "now must be timezone-aware; supply an explicit UTC clock"
        )
    if approval_expires_at.tzinfo is None or (
        approval_expires_at.tzinfo.utcoffset(approval_expires_at) is None
    ):
        raise CleanupBindingError(
            "naive_approval_expires_at", "approval_expires_at must be timezone-aware"
        )
    if now >= approval_expires_at:
        raise CleanupBindingError(
            "approval_window_expired",
            (
                f"approval window closed at {approval_expires_at.isoformat()}; "
                f"now={now.isoformat()}"
            ),
        )
    if session_id.strip() != approval_session_id.strip():
        raise CleanupBindingError(
            "cross_session_carry_over_blocked",
            "an approval issued to another session is not reusable here",
        )
    return CleanupBinding(
        purpose=CLEANUP_PURPOSE,
        contract_version=contract_version,
        forecast_id=forecast_id,
        sample_ref=sample_ref,
        approval_id=approval_id,
        approval_hash=approval_hash,
        approval_expires_at=approval_expires_at,
        approval_session_id=approval_session_id,
        mission_id=mission_id,
        account_mode=account_mode,
        client_order_id=client_order_id,
        lifecycle_correlation_id=lifecycle_correlation_id,
    )


__all__ = [
    "CLEANUP_PURPOSE",
    "CleanupBinding",
    "CleanupBindingError",
    "build_cleanup_binding",
]
