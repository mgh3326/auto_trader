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

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

CONTRACT_VERSION = "v1"


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


class ExecutionGuard(BaseModel):
    """Approval / execution gating fields shared by readiness, preview, and event models.

    Defaults are conservative. ``bool`` (not ``Literal[False]``) so future
    broker-submit code can flip values; the validator below keeps the
    invariant that any blocking reason forces ``execution_allowed=False``.
    """

    execution_allowed: bool = False
    approval_required: bool = True
    blocking_reasons: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _enforce_block_when_blocking_reasons(self) -> "ExecutionGuard":
        if self.blocking_reasons and self.execution_allowed:
            raise ValueError(
                "execution_allowed must be False when blocking_reasons is non-empty"
            )
        return self


class ExecutionReadiness(BaseModel):
    """Whether a given (account_mode, execution_source) is ready to submit orders right now."""

    contract_version: Literal["v1"] = "v1"
    account_mode: AccountMode
    execution_source: ExecutionSource
    is_ready: bool = False
    guard: ExecutionGuard = Field(default_factory=ExecutionGuard)
    checked_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ready_implies_no_blocking(self) -> "ExecutionReadiness":
        if self.is_ready and self.guard.blocking_reasons:
            raise ValueError(
                "is_ready cannot be True while guard.blocking_reasons is non-empty"
            )
        return self


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
    "ExecutionGuard",
    "ExecutionReadiness",
]
