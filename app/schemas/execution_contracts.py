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
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, NewType

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

CONTRACT_VERSION = "v1"


AccountMode = Literal["kis_live", "kis_mock", "alpaca_paper", "db_simulated"]
ACCOUNT_MODES: frozenset[str] = frozenset(
    {"kis_live", "kis_mock", "alpaca_paper", "db_simulated"}
)

ExecutionSource = Literal["preopen", "watch", "manual", "websocket", "reconciler"]
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
    "cancelled",
    "canceled",
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
        "cancelled",
        "canceled",
    }
)

# Terminal: order has reached a final outcome that does not change without
# explicit operator action. ``fill`` is intentionally NOT terminal because
# follow-up reconcilers may still need to confirm holdings/position state and
# emit ``reconciled``. ``anomaly`` is also intentionally NOT terminal — it means
# "needs operator review", which is a hand-off, not a conclusion.
TERMINAL_LIFECYCLE_STATES: frozenset[str] = frozenset(
    {"reconciled", "failed", "stale", "cancelled", "canceled"}
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
    def _enforce_block_when_blocking_reasons(self) -> ExecutionGuard:
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
    def _ready_implies_no_blocking(self) -> ExecutionReadiness:
        if self.is_ready and self.guard.blocking_reasons:
            raise ValueError(
                "is_ready cannot be True while guard.blocking_reasons is non-empty"
            )
        return self


class OrderPreviewLine(BaseModel):
    """A single previewed broker order line. Shared shape for basket previews and intent previews."""

    contract_version: Literal["v1"] = "v1"
    symbol: str
    market: str
    side: Literal["buy", "sell"]
    account_mode: AccountMode
    execution_source: ExecutionSource
    lifecycle_state: OrderLifecycleState = "previewed"
    quantity: Decimal | None = None
    limit_price: Decimal | None = None
    notional: Decimal | None = None
    currency: str | None = None
    guard: ExecutionGuard = Field(default_factory=ExecutionGuard)
    rationale: list[str] = Field(default_factory=list)
    correlation_id: str | None = None


class OrderBasketPreview(BaseModel):
    """A previewed basket of lines for one (account_mode, execution_source)."""

    contract_version: Literal["v1"] = "v1"
    account_mode: AccountMode
    execution_source: ExecutionSource
    readiness: ExecutionReadiness
    lines: list[OrderPreviewLine] = Field(default_factory=list)
    basket_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _lines_must_match_basket(self) -> OrderBasketPreview:
        for idx, line in enumerate(self.lines):
            if line.account_mode != self.account_mode:
                raise ValueError(
                    f"lines[{idx}].account_mode ({line.account_mode!r}) must match basket "
                    f"({self.account_mode!r})"
                )
            if line.execution_source != self.execution_source:
                raise ValueError(
                    f"lines[{idx}].execution_source ({line.execution_source!r}) must match basket "
                    f"({self.execution_source!r})"
                )
        return self


class OrderLifecycleEvent(BaseModel):
    """Vocabulary-shaped lifecycle event emitted by reconciler / websocket / broker code.

    ``detail`` carries broker-raw payload and is intentionally untyped; each
    follow-up branch fills it in its own format.
    """

    contract_version: Literal["v1"] = "v1"
    account_mode: AccountMode
    execution_source: ExecutionSource
    state: OrderLifecycleState
    occurred_at: datetime
    broker_order_id: str | None = None
    correlation_id: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# ROB-1259 J1 — frozen vocabulary only. These definitions deliberately live in
# the existing shared execution-contract leaf rather than creating a second
# generic execution schema module. They do not select a broker, a profile, a
# scheduler, or an order path.
J1NonBlank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
J1JsonObject = dict[str, Any]
TimingOwner = NewType("TimingOwner", str)


class LaneStatus(StrEnum):
    """The complete ROB-1259 J1 lane-status allowlist."""

    AUTO_ENABLED = "AUTO_ENABLED"
    AUTO_READY = "AUTO_READY"
    AUTO_READY_BLOCKED_BY_POLICY = "AUTO_READY_BLOCKED_BY_POLICY"
    AUTO_READY_BLOCKED_BY_LIFECYCLE = "AUTO_READY_BLOCKED_BY_LIFECYCLE"
    AUTO_READY_BLOCKED_BY_ACCOUNT_STATE = "AUTO_READY_BLOCKED_BY_ACCOUNT_STATE"
    AUTO_READY_BLOCKED_BY_SCHEDULER = "AUTO_READY_BLOCKED_BY_SCHEDULER"
    OBSERVATION_TEMPORARY = "OBSERVATION_TEMPORARY"
    SHADOW_ONLY = "SHADOW_ONLY"
    DISABLED_NO_STRATEGY = "DISABLED_NO_STRATEGY"
    NOT_READY = "NOT_READY"
    UNKNOWN = "UNKNOWN"


LaneState = LaneStatus
LANE_STATUSES: frozenset[str] = frozenset(status.value for status in LaneStatus)
LANE_STATES = LANE_STATUSES


class ActivationStatus(StrEnum):
    """Signed activation state kept distinct from a lane status."""

    READY_FOR_MOCK_DEPLOYMENT = "READY_FOR_MOCK_DEPLOYMENT"


ACTIVATION_STATUSES: frozenset[str] = frozenset(
    status.value for status in ActivationStatus
)


class LaneRole(StrEnum):
    """A registry role is one signed value, never a composite value."""

    PRIMARY_AUTO = "PRIMARY_AUTO"
    AUTO_MIRROR = "AUTO_MIRROR"
    BROKER_REGRESSION = "BROKER_REGRESSION"
    EXECUTION_AUTO = "EXECUTION_AUTO"


LANE_ROLES: frozenset[str] = frozenset(role.value for role in LaneRole)


class SchedulerOwner(StrEnum):
    """The exact scheduler-owner vocabulary from the integration contract."""

    TASKIQ = "taskiq"
    PREFECT = "prefect"
    ORCH = "orch"
    MANUAL = "manual"
    DISABLED = "disabled"


SCHEDULER_OWNERS: frozenset[str] = frozenset(owner.value for owner in SchedulerOwner)

CurrencyAlignmentError = Literal[
    "currency_conversion_not_authorized", "lane_quote_currency_mismatch"
]
CURRENCY_ALIGNMENT_ERROR_CODES: frozenset[CurrencyAlignmentError] = frozenset(
    {"currency_conversion_not_authorized", "lane_quote_currency_mismatch"}
)


class EvidenceTier(StrEnum):
    """J0 audit claim tier: directly observed, reasoned, or not verified."""

    FACT = "FACT"
    INFERENCE = "INFERENCE"
    UNVERIFIED = "UNVERIFIED"


EVIDENCE_TIERS: frozenset[str] = frozenset(tier.value for tier in EvidenceTier)


class _J1FrozenContract(BaseModel):
    """Strict, side-effect-free base for the three ROB-1259 J1 records."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class DecisionIntent(_J1FrozenContract):
    """One stable policy/strategy decision before account-specific planning."""

    decision_intent_id: J1NonBlank
    policy_version: J1NonBlank
    policy_version_hash: J1NonBlank
    decision_timestamp: datetime
    market_data_cutoff: datetime
    symbol: J1NonBlank
    side: Literal["buy", "sell"]
    target_notional: Decimal = Field(gt=0, allow_inf_nan=False)
    target_notional_currency: Literal["KRW", "USD", "USDT"]
    limit_policy: J1JsonObject
    expiry_policy: J1JsonObject
    rationale: J1NonBlank

    @field_validator("decision_timestamp", "market_data_cutoff")
    @classmethod
    def _timestamps_must_be_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError("timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _market_data_must_not_follow_the_decision(self) -> DecisionIntent:
        if self.market_data_cutoff > self.decision_timestamp:
            raise ValueError("market_data_cutoff must not be after decision_timestamp")
        return self


class ExecutionPlan(_J1FrozenContract):
    """One account-specific plan derived from a DecisionIntent."""

    execution_plan_id: J1NonBlank
    decision_intent_id: J1NonBlank
    lane_id: J1NonBlank
    broker: J1NonBlank
    account_profile: J1NonBlank
    account_mode: J1NonBlank
    normalized_symbol: J1NonBlank
    quantity: Decimal = Field(gt=0, allow_inf_nan=False)
    limit_price: Decimal | None
    quote_currency: Literal["KRW", "USD", "USDT"]
    tick_rounding: J1JsonObject
    session: J1NonBlank | None
    time_in_force: J1NonBlank | None
    min_order_validation: J1JsonObject
    risk_caps: J1JsonObject

    @field_validator("limit_price")
    @classmethod
    def _limit_price_must_be_positive_when_present(
        cls, value: Decimal | None
    ) -> Decimal | None:
        if value is not None and (not value.is_finite() or value <= 0):
            raise ValueError("limit_price must be finite and positive when present")
        return value


def validate_plan_currency_alignment(
    decision_intent: DecisionIntent, execution_plan: ExecutionPlan
) -> None:
    """Reject a decision/plan currency mismatch without conversion or I/O."""

    if decision_intent.target_notional_currency != execution_plan.quote_currency:
        raise ValueError("currency_conversion_not_authorized")


def create_execution_plan(
    decision_intent: DecisionIntent, /, **plan_values: Any
) -> ExecutionPlan:
    """Create a plan only when its supplied currency exactly matches the intent."""

    if plan_values.get("quote_currency") != decision_intent.target_notional_currency:
        raise ValueError("currency_conversion_not_authorized")
    return ExecutionPlan(**plan_values)


class OrderAttempt(_J1FrozenContract):
    """One attempt identity; broker identifiers may be absent before acknowledgement."""

    order_attempt_id: J1NonBlank
    execution_plan_id: J1NonBlank
    cycle_id: J1NonBlank
    idempotency_key: J1NonBlank
    broker_client_order_id: J1NonBlank | None
    broker_order_id: J1NonBlank | None


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
    "OrderPreviewLine",
    "OrderBasketPreview",
    "OrderLifecycleEvent",
    "TimingOwner",
    "LaneStatus",
    "LaneState",
    "LANE_STATUSES",
    "LANE_STATES",
    "ActivationStatus",
    "ACTIVATION_STATUSES",
    "LaneRole",
    "LANE_ROLES",
    "SchedulerOwner",
    "SCHEDULER_OWNERS",
    "CurrencyAlignmentError",
    "CURRENCY_ALIGNMENT_ERROR_CODES",
    "EvidenceTier",
    "EVIDENCE_TIERS",
    "DecisionIntent",
    "ExecutionPlan",
    "validate_plan_currency_alignment",
    "create_execution_plan",
    "OrderAttempt",
]
