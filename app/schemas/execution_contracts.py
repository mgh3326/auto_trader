"""Shared execution / order preview / lifecycle vocabulary (ROB-100 foundation).

Pure additive contract. This module defines the shared schema/types used by
follow-up parallel branches:

* preopen execution review panel and basket preview UI
* KIS mock order lifecycle and reconciliation worker
* watch order-intent MVP
* KIS websocket live/mock event tagging

This module MUST stay a leaf:
* It does not import any other ``app.*`` module.
* No existing ``app.*`` module imports it as part of ROB-100. Follow-up branches
  consume it on their own schedule (see design spec
  ``docs/superpowers/specs/2026-05-04-rob-100-execution-contracts-design.md``).

Defaults are conservative: ``execution_allowed=False``, ``approval_required=True``,
``is_ready=False``. Validators enforce that blocking reasons and "allowed/ready"
states cannot coexist.
"""

from __future__ import annotations

CONTRACT_VERSION = "v1"

from typing import Literal

AccountMode = Literal["kis_live", "kis_mock", "alpaca_paper", "db_simulated"]
ACCOUNT_MODES: frozenset[str] = frozenset(
    {"kis_live", "kis_mock", "alpaca_paper", "db_simulated"}
)

ExecutionSource = Literal[
    "preopen", "watch", "manual", "websocket", "reconciler"
]
EXECUTION_SOURCES: frozenset[str] = frozenset(
    {"preopen", "watch", "manual", "websocket", "reconciler"}
)

OrderLifecycleState = Literal[
    "planned",
    "previewed",
    "submitted",
    "accepted",
    "pending",
    "fill",
    "reconciled",
    "stale",
    "failed",
    "anomaly",
]
ORDER_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {
        "planned",
        "previewed",
        "submitted",
        "accepted",
        "pending",
        "fill",
        "reconciled",
        "stale",
        "failed",
        "anomaly",
    }
)

# Terminal: order has reached a final outcome that does not change without
# explicit operator action. ``anomaly`` is intentionally NOT terminal — it
# means "needs operator review", which is a hand-off, not a conclusion.
TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {"fill", "reconciled", "failed", "stale"}
)

# In-flight: order has been sent or acknowledged by the broker and is
# expected to transition without operator input.
IN_FLIGHT_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {"submitted", "accepted", "pending"}
)


def is_terminal_state(state: OrderLifecycleState) -> bool:
    return state in TERMINAL_LIFECYCLE_STATES


def is_in_flight_state(state: OrderLifecycleState) -> bool:
    return state in IN_FLIGHT_LIFECYCLE_STATES


__all__ = [
    "CONTRACT_VERSION",
    "AccountMode",
    "ACCOUNT_MODES",
    "ExecutionSource",
    "EXECUTION_SOURCES",
    "OrderLifecycleState",
    "ORDER_LIFECYCLE_STATES",
    "TERMINAL_LIFECYCLE_STATES",
    "IN_FLIGHT_LIFECYCLE_STATES",
    "is_terminal_state",
    "is_in_flight_state",
]
