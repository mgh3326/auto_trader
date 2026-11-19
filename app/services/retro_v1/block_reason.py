"""Why a proposal or rung has not been submitted (retro/v1 ``block_reason``).

Pure and read-only: the reason is derived from columns already stored on the
proposal and rung rows -- ``lifecycle_state``, the approval-dispatch ledger
columns, the ``auto_approve_rejections`` / ``auto_approved`` keys of
``source_asof`` and a rung's ``state`` / ``void_reason_group``. Nothing here
writes, and nothing imports the order-proposal machinery (the retro/v1 guards
forbid it), so the few stored strings it reads are matched literally and the
table in ``docs/agent-contracts/mcp-and-analysis-contracts.md`` is the
contract for them.

Only a closed code and its basis leave this module. The stored ``void_reason``
text, the dispatch ``failure_code`` detail and any broker message are used for
matching and never copied: a row whose evidence does not decide a code gets
no ``block_reason`` at all (never an empty string or a guess).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

#: The closed vocabulary (the contract's ``BlockReason`` enum).
BLOCK_REASONS: Final[tuple[str, ...]] = (
    "awaiting_human_approval",
    "auto_approve_demoted",
    "deferred_session_closed",
    "no_executable_window",
    "calendar_unknown",
    "invalid_valid_until",
    "expired_valid_until",
    "voided",
    "superseded",
    "denied_by_operator",
    "rejected_policy_guard",
    "rejected_broker",
    "duplicate_intent",
)
BLOCK_BASES: Final[tuple[str, ...]] = ("recorded", "inference")

BlockReason = tuple[str, str]  # (code, basis)

#: ``approval_dispatch_failure_code`` is ``<WINDOW CODE>/<detail>``; only the
#: code before the slash is read (the approval-window contract's enum).
_WINDOW_CODE_REASONS: Final[dict[str, str]] = {
    "DEFER_SESSION_CLOSED": "deferred_session_closed",
    "NO_EXECUTABLE_WINDOW": "no_executable_window",
    "CALENDAR_UNKNOWN": "calendar_unknown",
    "INVALID_VALID_UNTIL": "invalid_valid_until",
    "EXPIRED": "expired_valid_until",
}
_FAILED_DISPATCH_STATES: Final[frozenset[str]] = frozenset(
    {"failed", "partial_failed", "failed_superseded"}
)
#: Card kinds that put the decision in a person's hands (``auto_veto`` is an
#: already auto-approved order with a veto window, not a wait for approval).
_HUMAN_CARD_KINDS: Final[frozenset[str]] = frozenset(
    {"manual", "reconfirm", "loss_cut_confirmation"}
)
#: Exact stored ``void_reason`` values of an explicit person's denial.
_DENIAL_REASONS: Final[frozenset[str]] = frozenset({"operator_denied", "telegram_deny"})
_EXPIRED_VALID_UNTIL_PREFIX: Final = "expired_valid_until"
_UNSENT_RUNG_STATES: Final[frozenset[str]] = frozenset(
    {"pending_approval", "needs_reconfirm"}
)
_GROUP_REJECTIONS: Final[dict[str, str]] = {
    "policy_guard": "rejected_policy_guard",
    "broker_rejection": "rejected_broker",
    "provider_throttle": "rejected_broker",
    "duplicate_pending_intent": "duplicate_intent",
}


def _text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _norm(value: Any) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _window_reason(proposal: Any) -> str | None:
    """The approval-window block the last dispatch attempt recorded."""
    if _text(getattr(proposal, "approval_dispatch_state", None)) not in (
        _FAILED_DISPATCH_STATES
    ):
        return None
    code = _text(getattr(proposal, "approval_dispatch_failure_code", None))
    if code is None:
        return None
    return _WINDOW_CODE_REASONS.get(code.split("/", 1)[0])


def _auto_approve_demoted(proposal: Any) -> bool:
    """Auto-approve was evaluated, refused, and the order did not auto-send."""
    source = getattr(proposal, "source_asof", None)
    if not isinstance(source, Mapping) or "auto_approved" in source:
        return False
    attempts = source.get("auto_approve_rejections")
    if not isinstance(attempts, Sequence) or isinstance(attempts, (str, bytes)):
        return False
    return any(
        isinstance(attempt, Mapping)
        and isinstance(attempt.get("rungs"), Sequence)
        and not isinstance(attempt.get("rungs"), (str, bytes))
        and len(attempt["rungs"]) > 0
        for attempt in attempts
    )


def _pre_send_reason(proposal: Any) -> BlockReason | None:
    """Why a still-``proposed`` proposal (and its unsent rungs) is waiting."""
    if _text(getattr(proposal, "lifecycle_state", None)) != "proposed":
        return None
    window = _window_reason(proposal)
    if window is not None:
        return window, "recorded"
    if _auto_approve_demoted(proposal):
        return "auto_approve_demoted", "recorded"
    if (
        _text(getattr(proposal, "approval_dispatch_state", None)) == "sent_current"
        and _text(getattr(proposal, "approval_dispatch_card_kind", None))
        in _HUMAN_CARD_KINDS
        and getattr(proposal, "approval_nonce_used_at", None) is None
    ):
        return "awaiting_human_approval", "recorded"
    return None


def _never_sent(rung: Any) -> bool:
    return (
        getattr(rung, "broker_order_id", None) is None
        and getattr(rung, "idempotency_key", None) is None
    )


def proposal_block_reason(proposal: Any, rungs: Sequence[Any]) -> BlockReason | None:
    """The block reason of a proposal row, or ``None`` when none is derivable."""
    state = _text(getattr(proposal, "lifecycle_state", None))
    if state == "proposed":
        return _pre_send_reason(proposal)
    reason = _norm(getattr(proposal, "void_reason", None))
    if state == "expired":
        if reason.startswith(_EXPIRED_VALID_UNTIL_PREFIX):
            return "expired_valid_until", "recorded"
        # An expiry sweep with no reason is the valid_until sweep unless an
        # order reached the broker (then it is a day-order expiry).
        if rungs and all(_never_sent(r) for r in rungs):
            return "expired_valid_until", "inference"
        return None
    if state in {"voided", "rejected"}:
        if reason in _DENIAL_REASONS:
            return "denied_by_operator", "recorded"
        return ("voided", "recorded") if state == "voided" else None
    if state == "superseded":
        return "superseded", "recorded"
    return None


def rung_block_reason(proposal: Any, rung: Any) -> BlockReason | None:
    """The block reason of a rung row, or ``None`` when none is derivable."""
    state = _text(getattr(rung, "state", None))
    if state in _UNSENT_RUNG_STATES:
        return _pre_send_reason(proposal)
    reason = _norm(getattr(rung, "void_reason", None))
    if state == "expired":
        if reason.startswith(_EXPIRED_VALID_UNTIL_PREFIX):
            return "expired_valid_until", "recorded"
        if _never_sent(rung):
            return "expired_valid_until", "inference"
        return None
    if state in {"voided", "voided_local_stale"}:
        if reason in _DENIAL_REASONS:
            return "denied_by_operator", "recorded"
        return "voided", "recorded"
    if state == "superseded":
        return "superseded", "recorded"
    if state == "rejected":
        if reason in _DENIAL_REASONS:
            return "denied_by_operator", "recorded"
        group = _text(getattr(rung, "void_reason_group", None))
        code = _GROUP_REJECTIONS.get(group) if group else None
        return (code, "recorded") if code else None
    return None


def block_fields(block: BlockReason | None) -> dict[str, str]:
    """The optional row keys: present only with a value."""
    if block is None:
        return {}
    code, basis = block
    return {"block_reason": code, "block_reason_basis": basis}
