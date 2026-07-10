"""Pure, dependency-free state machine for order_proposal_rungs (ROB-816).

stdlib only — no broker/DB/network imports. The service calls assert_rung_transition
before every rung state mutation; the DB CheckConstraint only validates the string
bag, the transition graph lives here.
"""

from __future__ import annotations

from app.services.order_proposals.errors import OrderProposalInvalidStateTransition

_ALLOWED: dict[str, frozenset[str]] = {
    "draft": frozenset({"pending_approval", "voided"}),
    "pending_approval": frozenset(
        {"revalidating", "rejected", "voided", "voided_local_stale", "superseded"}
    ),
    "revalidating": frozenset(
        {"approved", "needs_reconfirm", "pending_approval", "superseded", "voided"}
    ),
    "needs_reconfirm": frozenset(
        {"pending_approval", "rejected", "superseded", "voided"}
    ),
    "approved": frozenset({"submitting", "superseded", "voided"}),
    "submitting": frozenset({"acked", "resting", "rejected", "unverified"}),
    "acked": frozenset({"filled", "partially_filled", "cancelled", "unverified"}),
    "resting": frozenset(
        {"filled", "partially_filled", "cancelled", "expired", "unverified"}
    ),
    "partially_filled": frozenset({"filled", "cancelled", "expired", "unverified"}),
    "unverified": frozenset(
        {"filled", "partially_filled", "cancelled", "expired", "rejected"}
    ),
    # terminals
    "filled": frozenset(),
    "cancelled": frozenset(),
    "expired": frozenset(),
    "rejected": frozenset(),
    "voided": frozenset(),
    "voided_local_stale": frozenset(),
    "superseded": frozenset(),
}

RUNG_STATES: frozenset[str] = frozenset(_ALLOWED.keys())
PROPOSAL_TERMINAL_STATES: frozenset[str] = frozenset(
    s for s, nxt in _ALLOWED.items() if not nxt
)

# Group-level rollup states (order_proposals.lifecycle_state) — coarser than rung states.
GROUP_STATES: frozenset[str] = frozenset(
    {
        "proposed",
        "approved",
        "partially_submitted",
        "submitted",
        "terminal",
        "rejected",
        "expired",
        "voided",
        "superseded",
    }
)


def is_terminal(state: str) -> bool:
    return state in PROPOSAL_TERMINAL_STATES


def assert_rung_transition(current: str, new: str) -> None:
    if current not in _ALLOWED:
        raise OrderProposalInvalidStateTransition(f"unknown rung state {current!r}")
    if new not in RUNG_STATES:
        raise OrderProposalInvalidStateTransition(f"unknown target state {new!r}")
    allowed = _ALLOWED[current]
    if new not in allowed:
        raise OrderProposalInvalidStateTransition(
            f"{current!r} -> {new!r} not allowed "
            f"(allowed from {current!r}: {sorted(allowed)})"
        )
