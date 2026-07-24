"""Authoritative approval-publication, ownership, and callback contracts."""

from __future__ import annotations

import base64
import hashlib
import json
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from app.telegram_contract import TelegramErrorClassification, TelegramMethodResult


class ApprovalDispatchState(StrEnum):
    """Closed attempt/workflow states used by every approval side effect."""

    PENDING = "pending"
    SENT_CURRENT = "sent_current"
    SENT_SUPERSEDED = "sent_superseded"
    FAILED = "failed"
    PARTIAL_FAILED = "partial_failed"
    FAILED_SUPERSEDED = "failed_superseded"

    @property
    def approvable(self) -> bool:
        return self is ApprovalDispatchState.SENT_CURRENT

    @property
    def caller_state(self) -> str:
        if self is ApprovalDispatchState.SENT_CURRENT:
            return "sent"
        if self in {
            ApprovalDispatchState.SENT_SUPERSEDED,
            ApprovalDispatchState.FAILED_SUPERSEDED,
        }:
            return "superseded"
        return self.value


class ApprovalCardKind(StrEnum):
    MANUAL = "manual"
    RECONFIRM = "reconfirm"
    AUTO_VETO = "auto_veto"
    LOSS_CUT_CONFIRMATION = "loss_cut_confirmation"
    BATCH = "batch"


_ACTIONS_BY_CARD_KIND: dict[ApprovalCardKind, frozenset[str]] = {
    ApprovalCardKind.MANUAL: frozenset({"op", "dn"}),
    ApprovalCardKind.RECONFIRM: frozenset({"op", "dn"}),
    ApprovalCardKind.AUTO_VETO: frozenset({"vc"}),
    ApprovalCardKind.LOSS_CUT_CONFIRMATION: frozenset({"lc", "dn"}),
    ApprovalCardKind.BATCH: frozenset({"ba"}),
}


@dataclass(frozen=True, slots=True)
class DispatchBinding:
    """Exact immutable snapshot to which a published callback is bound."""

    attempt_id: uuid.UUID
    card_kind: ApprovalCardKind
    membership_revision: int
    membership_digest: str


@dataclass(frozen=True, slots=True)
class CallbackEnvelope:
    action: str
    subject_short: str
    attempt_id: uuid.UUID
    membership_revision: int
    membership_digest: str
    nonce: str


@dataclass(frozen=True, slots=True)
class CallbackGateSnapshot:
    subject_short: str
    state: ApprovalDispatchState
    attempt_id: uuid.UUID | None
    card_kind: ApprovalCardKind | None
    membership_revision: int | None
    membership_digest: str | None
    nonce: str | None
    nonce_used: bool


def assert_callback_gate(
    *, snapshot: CallbackGateSnapshot, callback: CallbackEnvelope
) -> None:
    """Apply the one fail-closed gate shared by every nonce consumer."""
    if snapshot.subject_short != callback.subject_short:
        raise ValueError("approval_callback_subject_mismatch")
    if snapshot.state is not ApprovalDispatchState.SENT_CURRENT:
        raise ValueError(f"approval_dispatch_{snapshot.state.value}")
    if snapshot.attempt_id != callback.attempt_id:
        raise ValueError("approval_dispatch_attempt_mismatch")
    if snapshot.nonce != callback.nonce:
        raise ValueError("nonce_mismatch")
    if snapshot.nonce_used:
        raise ValueError("nonce_replay")
    if snapshot.membership_revision != callback.membership_revision:
        raise ValueError("approval_membership_revision_mismatch")
    if snapshot.membership_digest != callback.membership_digest:
        raise ValueError("approval_membership_digest_mismatch")
    if (
        snapshot.card_kind is None
        or callback.action not in _ACTIONS_BY_CARD_KIND[snapshot.card_kind]
    ):
        raise ValueError("approval_card_action_mismatch")


def build_membership_digest(
    *,
    card_kind: ApprovalCardKind,
    membership_revision: int,
    members: Sequence[dict[str, Any]],
) -> str:
    """Return the compact stored digest used verbatim in callback data."""
    canonical = json.dumps(
        {
            "card_kind": card_kind.value,
            "membership_revision": membership_revision,
            "members": list(members),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    digest = hashlib.sha256(canonical).digest()[:9]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def build_proposal_dispatch_binding(
    *,
    proposal_id: uuid.UUID,
    nonce: str | None,
    attempt_id: uuid.UUID,
    card_kind: ApprovalCardKind,
    current_membership_revision: int | None,
) -> DispatchBinding:
    """Build the next immutable single-proposal publication binding."""
    revision = (current_membership_revision or 0) + 1
    digest = build_membership_digest(
        card_kind=card_kind,
        membership_revision=revision,
        members=[{"proposal_id": str(proposal_id), "approval_nonce": nonce}],
    )
    return DispatchBinding(
        attempt_id=attempt_id,
        card_kind=card_kind,
        membership_revision=revision,
        membership_digest=digest,
    )


@dataclass(frozen=True, slots=True)
class ApprovalPublication:
    """Physical publication receipt that is not yet workflow success."""

    card_published: bool
    partial: bool
    message_id: int | None
    status_code: int | None
    error_code: int | None
    error_classification: TelegramErrorClassification | None
    payload_chars: int
    failure_code: str | None

    @classmethod
    def failed(
        cls,
        *,
        payload_chars: int,
        failure_code: str,
        partial: bool = False,
        method_result: TelegramMethodResult | None = None,
    ) -> ApprovalPublication:
        return cls(
            card_published=False,
            partial=partial,
            message_id=None,
            status_code=(
                method_result.status_code if method_result is not None else None
            ),
            error_code=(
                method_result.error_code if method_result is not None else None
            ),
            error_classification=(
                method_result.error_classification
                if method_result is not None
                else None
            ),
            payload_chars=payload_chars,
            failure_code=failure_code,
        )

    @classmethod
    def published(
        cls, *, payload_chars: int, method_result: TelegramMethodResult
    ) -> ApprovalPublication:
        if not method_result.ok or method_result.message_id is None:
            raise ValueError("published receipt requires a successful Telegram result")
        return cls(
            card_published=True,
            partial=False,
            message_id=method_result.message_id,
            status_code=method_result.status_code,
            error_code=None,
            error_classification=None,
            payload_chars=payload_chars,
            failure_code=None,
        )


@dataclass(frozen=True, slots=True)
class TelegramDispatchResult:
    """Durable caller result derived from the final typed workflow state."""

    state: ApprovalDispatchState
    message_id: int | None
    status_code: int | None
    error_code: int | None
    error_classification: TelegramErrorClassification | None
    payload_chars: int
    failure_code: str | None

    @property
    def ok(self) -> bool:
        return self.state is ApprovalDispatchState.SENT_CURRENT

    @property
    def approvable(self) -> bool:
        return self.state.approvable

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["state"] = self.state.caller_state
        payload["ok"] = self.ok
        if self.error_classification is not None:
            payload["error_classification"] = self.error_classification.value
        return payload

    @classmethod
    def from_publication(
        cls,
        publication: ApprovalPublication,
        *,
        state: ApprovalDispatchState,
        failure_code: str | None = None,
    ) -> TelegramDispatchResult:
        if state is ApprovalDispatchState.SENT_CURRENT:
            resolved_failure = None
        elif failure_code is not None:
            resolved_failure = failure_code
        else:
            resolved_failure = publication.failure_code
        return cls(
            state=state,
            message_id=publication.message_id,
            status_code=publication.status_code,
            error_code=publication.error_code,
            error_classification=publication.error_classification,
            payload_chars=publication.payload_chars,
            failure_code=resolved_failure,
        )


__all__ = [
    "ApprovalCardKind",
    "ApprovalDispatchState",
    "ApprovalPublication",
    "CallbackEnvelope",
    "CallbackGateSnapshot",
    "DispatchBinding",
    "TelegramDispatchResult",
    "assert_callback_gate",
    "build_membership_digest",
    "build_proposal_dispatch_binding",
]
