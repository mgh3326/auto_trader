"""Pure void/expire authorization classifier for order proposals (ROB-1238).

stdlib only — no broker/DB/network/clock imports. ``void_proposal`` calls
``authorize_void`` before it mutates any rung; the caller supplies every input
and the decision is a pure function of them, so the rule is testable without a
session.

Why this exists
---------------
``order_proposal_void`` was blocked in every ``route_request`` lane, so nobody
could retire a phantom proposal (27 stale rows by 2026-08-10). The fix is NOT
to open void wholesale: proposals are a lane-shared surface, and a broad
allowance would let one session void another lane's live proposal.

Authorization is therefore narrowed to three disjoint authorities, in
precedence order:

``self_created``
    The requester is the agent that created the proposal. Ownership is compared
    against ``source_asof["creator_agent_id"]``, which the *server* records from
    the MCP caller-identity middleware at create time — never from a tool
    argument, so a caller cannot assert someone else's identity.

``server_loss_guard_invalid``
    The server itself observed the proposal fail the loss-sell guard during a
    dispatch-time revalidation and durably recorded that verdict. Caller-supplied
    evidence (e.g. ``lot_context``) never grants this authority.

``server_expired``
    The server compares ``valid_until`` against ``now``. This is the lazy
    convergence path that replaces a sweep scheduler: an operator (or any
    session) touching a long-dead proposal retires it, and the retirement state
    is ``expired`` rather than ``voided`` because expiry is what the server
    actually proved.

Anything else is refused. A live proposal belonging to another lane has no
authority and stays untouchable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

VoidAuthority = Literal[
    "self_created",
    "server_loss_guard_invalid",
    "server_expired",
]

#: Terminal rung/group state each authority converges to.
_AUTHORITY_TERMINAL_STATE: dict[str, str] = {
    "self_created": "voided",
    "server_loss_guard_invalid": "voided",
    "server_expired": "expired",
}

#: The only ``source`` value that makes a recorded loss-guard verdict
#: server-confirmed. Anything else (including a caller-injected payload that
#: guesses at the key name) is treated as absent.
SERVER_LOSS_GUARD_SOURCE = "server_revalidation"

#: ``source_asof`` keys this module reads. Kept here so the service and the
#: tests agree on one spelling.
CREATOR_AGENT_ID_KEY = "creator_agent_id"
LOSS_GUARD_VERDICT_KEY = "loss_guard_verdict"


@dataclass(frozen=True)
class VoidAuthorizationDecision:
    """Outcome of the void authorization check."""

    allowed: bool
    authority: VoidAuthority | None
    terminal_state: str | None
    reason_code: str
    detail: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "allowed": self.allowed,
            "authority": self.authority,
            "terminal_state": self.terminal_state,
            "reason_code": self.reason_code,
            "detail": dict(self.detail),
        }


def _clean(value: object) -> str | None:
    """Normalize an identity to a comparable, non-empty string."""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


def extract_creator_agent_id(source_asof: object) -> str | None:
    """Read the server-recorded creator identity out of ``source_asof``."""
    if not isinstance(source_asof, dict):
        return None
    return _clean(source_asof.get(CREATOR_AGENT_ID_KEY))


def extract_loss_guard_violation(source_asof: object) -> dict[str, str] | None:
    """Return the server-recorded loss-guard violation, if there is one.

    Returns ``None`` unless the stored envelope is a dict that both marks
    ``violated is True`` and carries the server's own ``source`` marker. A
    truthy-but-not-``True`` value (``"yes"``, ``1``) does not count: this
    authority permits a mutation, so it fails closed on anything ambiguous.
    """
    if not isinstance(source_asof, dict):
        return None
    envelope = source_asof.get(LOSS_GUARD_VERDICT_KEY)
    if not isinstance(envelope, dict):
        return None
    if envelope.get("violated") is not True:
        return None
    if _clean(envelope.get("source")) != SERVER_LOSS_GUARD_SOURCE:
        return None
    detail = {"loss_guard_source": SERVER_LOSS_GUARD_SOURCE}
    for key in ("observed_at", "rung_index", "error"):
        value = envelope.get(key)
        if value is not None:
            detail[f"loss_guard_{key}"] = str(value)
    return detail


def is_server_confirmed_expired(
    valid_until: datetime | None,
    *,
    now: datetime,
) -> bool:
    """True when the server can prove the approval window has elapsed.

    A proposal with no ``valid_until`` is never expired by this rule — absence
    of a deadline is not evidence that one passed.
    """
    if valid_until is None:
        return False
    if valid_until.tzinfo is None or now.tzinfo is None:
        raise ValueError("valid_until and now must be timezone-aware")
    return now >= valid_until


def authorize_void(
    *,
    requester_agent_id: str | None,
    creator_agent_id: str | None,
    valid_until: datetime | None,
    now: datetime,
    loss_guard_violation: dict[str, str] | None = None,
) -> VoidAuthorizationDecision:
    """Decide whether this requester may retire this proposal.

    ``requester_agent_id`` and ``creator_agent_id`` must both be server-resolved.
    When either is unknown, ownership cannot be established and only the
    server-confirmed authorities remain — an unauthenticated caller can still
    retire provably-dead rows but can never touch a live one.
    """
    requester = _clean(requester_agent_id)
    creator = _clean(creator_agent_id)

    if requester is not None and creator is not None and requester == creator:
        return _allow(
            "self_created",
            {"requester_agent_id": requester, "creator_agent_id": creator},
        )

    if loss_guard_violation:
        return _allow("server_loss_guard_invalid", dict(loss_guard_violation))

    if is_server_confirmed_expired(valid_until, now=now):
        return _allow(
            "server_expired",
            {
                "valid_until": valid_until.isoformat() if valid_until else "",
                "observed_at": now.isoformat(),
            },
        )

    return VoidAuthorizationDecision(
        allowed=False,
        authority=None,
        terminal_state=None,
        reason_code="void_not_authorized",
        detail={
            "requester_agent_id": requester or "unknown",
            "creator_agent_id": creator or "unknown",
            "valid_until": valid_until.isoformat() if valid_until else "none",
            "observed_at": now.isoformat(),
            "hint": (
                "void is limited to a proposal you created, or one the server "
                "has itself confirmed expired or loss-guard-invalid"
            ),
        },
    )


def _allow(
    authority: VoidAuthority, detail: dict[str, str]
) -> VoidAuthorizationDecision:
    return VoidAuthorizationDecision(
        allowed=True,
        authority=authority,
        terminal_state=_AUTHORITY_TERMINAL_STATE[authority],
        reason_code=f"authorized:{authority}",
        detail=detail,
    )


__all__ = [
    "CREATOR_AGENT_ID_KEY",
    "LOSS_GUARD_VERDICT_KEY",
    "SERVER_LOSS_GUARD_SOURCE",
    "VoidAuthority",
    "VoidAuthorizationDecision",
    "authorize_void",
    "extract_creator_agent_id",
    "extract_loss_guard_violation",
    "is_server_confirmed_expired",
]
