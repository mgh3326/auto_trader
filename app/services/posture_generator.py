"""Pure posture-v1 stage-1 generator and coverage accounting (ROB-1106).

The generator consumes a captured holdings/quotes/policy-context snapshot and
returns declarative posture rows. It has no database, proposal, notification,
broker, or order dependency. Unknown policy combinations are intentionally
reported as unmapped instead of being forced into DISARMED: finding those holes
is the purpose of the first shadow stage.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.trading_policy import PosturePolicy


class PostureState(StrEnum):
    RESTING = "RESTING"
    CONDITIONAL = "CONDITIONAL"
    ARMED_DEFERRED = "ARMED_DEFERRED"
    DISARMED = "DISARMED"
    EXPIRED_REARMABLE = "EXPIRED_REARMABLE"


POSTURE_STATES: tuple[PostureState, ...] = tuple(PostureState)

Market = Literal["kr", "us", "crypto"]
Intent = Literal["buy", "sell"]
LevelStatus = Literal["fresh", "stale", "expired", "missing"]
LevelStrength = Literal["strong", "moderate", "weak", "unknown"]

_MAX_LIVE_DISTANCE_PCT: dict[str, Decimal] = {
    "kr": Decimal("15"),
    "us": Decimal("15"),
    "crypto": Decimal("20"),
}


class PostureDisabledError(RuntimeError):
    """Raised when a caller tries to bypass the default-off posture gate."""


class PostureHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    market: Market
    quantity: Decimal
    average_cost: Decimal | None = None


class PostureQuote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str = Field(min_length=1)
    market: Market
    price: Decimal
    observed_at: dt.datetime


class PosturePolicyContext(BaseModel):
    """Policy facts already resolved for one holding.

    ``level_role`` remains an open string on purpose. A new/unknown role must
    appear in ``unmapped_holdings`` until the five-state definition explicitly
    covers it; validation must not erase that measurement opportunity.
    """

    model_config = ConfigDict(extra="forbid")

    holding_id: str = Field(min_length=1)
    intent: Intent
    level_role: str | None = None
    level_price: Decimal | None = None
    level_status: LevelStatus = "missing"
    level_strength: LevelStrength = "unknown"
    price_touch_sufficient: bool | None = None
    evidence_ready: bool | None = None
    intent_approved: bool | None = None
    risk_eligible: bool | None = None
    economic_floor_met: bool | None = None
    rearm_allowed: bool = False
    block_reasons: list[str] = Field(default_factory=list)
    review_at: dt.datetime | None = None


class PostureGeneratorInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at: dt.datetime
    holdings: list[PostureHolding]
    quotes: list[PostureQuote]
    policy_contexts: list[PosturePolicyContext]

    @model_validator(mode="after")
    def validate_unique_input_keys(self) -> PostureGeneratorInput:
        holding_ids = [row.holding_id for row in self.holdings]
        if len(holding_ids) != len(set(holding_ids)):
            raise ValueError("holding_id must be unique")

        context_ids = [row.holding_id for row in self.policy_contexts]
        if len(context_ids) != len(set(context_ids)):
            raise ValueError("policy context holding_id must be unique")

        quote_keys = [(row.market, row.symbol.upper()) for row in self.quotes]
        if len(quote_keys) != len(set(quote_keys)):
            raise ValueError("quote market/symbol must be unique")
        return self


class PostureAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_id: str
    account_id: str
    symbol: str
    market: Market
    state: PostureState
    reason: str
    intent: Intent
    level_role: str | None
    quantity: Decimal
    current_price: Decimal | None
    level_price: Decimal | None
    target_distance_pct: Decimal | None
    review_at: dt.datetime | None
    policy_version: str
    policy_content_hash: str


class UnmappedHolding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    holding_id: str
    account_id: str
    symbol: str
    market: Market
    reason: str
    level_role: str | None = None


class PostureCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_holding_count: int
    mapped_holding_count: int
    unmapped_holding_count: int
    coverage_ratio: float
    state_counts: dict[PostureState, int]
    state_symbol_counts: dict[PostureState, int]

    @model_validator(mode="after")
    def validate_totals_and_five_states(self) -> PostureCoverage:
        expected = set(POSTURE_STATES)
        if set(self.state_counts) != expected:
            raise ValueError("state_counts must include exactly all five states")
        if set(self.state_symbol_counts) != expected:
            raise ValueError("state_symbol_counts must include exactly all five states")
        if sum(self.state_counts.values()) != self.mapped_holding_count:
            raise ValueError("state_counts total must equal mapped_holding_count")
        if (
            self.mapped_holding_count + self.unmapped_holding_count
            != self.input_holding_count
        ):
            raise ValueError("mapped + unmapped must equal input holding count")
        return self


class ShadowSafetyCounters(BaseModel):
    """Explicit zero-mutation evidence carried by every shadow artifact."""

    model_config = ConfigDict(extra="forbid")

    orders_created: Literal[0] = 0
    proposals_created: Literal[0] = 0
    broker_mutations: Literal[0] = 0


class PostureGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    captured_at: dt.datetime
    generated_at: dt.datetime
    policy_version: str
    policy_content_hash: str
    mode: Literal["shadow"] = "shadow"
    assignments: list[PostureAssignment]
    unmapped_holdings: list[UnmappedHolding]
    coverage: PostureCoverage
    safety: ShadowSafetyCounters = Field(default_factory=ShadowSafetyCounters)


def _distance_pct(level_price: Decimal, quote_price: Decimal) -> Decimal:
    return ((level_price / quote_price) - Decimal("1")) * Decimal("100")


def _unmapped_reason(
    *,
    holding: PostureHolding,
    context: PosturePolicyContext | None,
) -> str | None:
    if context is None:
        return "missing_policy_context"
    if context.level_role is None:
        return "missing_level_role"
    if context.level_role not in {
        "HARD_EXIT",
        "CONDITIONAL_EXIT",
        "HARD_INVALIDATION",
        "BUY_SUPPORT",
        "REFERENCE_ONLY",
    }:
        return f"state_definition_gap:unknown_level_role:{context.level_role}"
    if context.intent == "sell" and context.level_role == "BUY_SUPPORT":
        return "state_definition_gap:sell_intent_with_buy_support"
    if context.intent == "buy" and context.level_role in {
        "HARD_EXIT",
        "CONDITIONAL_EXIT",
        "HARD_INVALIDATION",
    }:
        return "state_definition_gap:buy_intent_with_exit_role"
    if (
        context.level_role == "HARD_EXIT"
        and context.level_status == "fresh"
        and context.price_touch_sufficient is None
    ):
        return "state_definition_gap:hard_exit_touch_policy_missing"
    if (
        context.level_role == "BUY_SUPPORT"
        and context.level_status == "fresh"
        and context.evidence_ready is not False
        and context.intent_approved is None
    ):
        return "state_definition_gap:buy_intent_approval_missing"
    if holding.market not in _MAX_LIVE_DISTANCE_PCT:
        return f"state_definition_gap:unsupported_market:{holding.market}"
    return None


def _classify(
    *,
    holding: PostureHolding,
    quote: PostureQuote | None,
    context: PosturePolicyContext,
) -> tuple[PostureState, str, Decimal | None]:
    if holding.quantity <= 0:
        return PostureState.DISARMED, "position_zero", None
    if context.block_reasons:
        return (
            PostureState.DISARMED,
            "policy_block:" + ",".join(sorted(set(context.block_reasons))),
            None,
        )
    if context.risk_eligible is False:
        return PostureState.DISARMED, "risk_ineligible", None
    if context.economic_floor_met is False:
        return PostureState.DISARMED, "economic_floor_not_met", None
    if context.level_status == "stale":
        return PostureState.DISARMED, "stale_level", None
    if context.level_status == "missing":
        return PostureState.DISARMED, "invalid_level_data", None
    if context.level_status == "expired":
        if context.rearm_allowed:
            return PostureState.EXPIRED_REARMABLE, "expired_level_rearmable", None
        return PostureState.DISARMED, "expired_level_not_rearmable", None
    if context.level_role == "REFERENCE_ONLY":
        return PostureState.DISARMED, "reference_only", None
    if context.evidence_ready is False:
        return PostureState.ARMED_DEFERRED, "pending_evidence", None
    if context.level_price is None or context.level_price <= 0:
        return PostureState.DISARMED, "invalid_level_data", None
    if quote is None or quote.price <= 0:
        return PostureState.DISARMED, "invalid_quote_data", None

    distance_pct = _distance_pct(context.level_price, quote.price)
    max_live_distance = _MAX_LIVE_DISTANCE_PCT[holding.market]

    if context.level_role == "CONDITIONAL_EXIT":
        return PostureState.CONDITIONAL, "conditional_exit", distance_pct
    if context.level_role == "HARD_INVALIDATION":
        return PostureState.CONDITIONAL, "hard_invalidation_review", distance_pct
    if context.level_role == "HARD_EXIT":
        if context.price_touch_sufficient is False:
            return (
                PostureState.CONDITIONAL,
                "hard_exit_requires_more_information",
                distance_pct,
            )
        if context.level_strength == "weak":
            return PostureState.CONDITIONAL, "weak_level", distance_pct
        if distance_pct > max_live_distance:
            return PostureState.CONDITIONAL, "target_beyond_live_band", distance_pct
        return PostureState.RESTING, "hard_exit_touch_sufficient", distance_pct
    if context.level_role == "BUY_SUPPORT":
        if context.intent_approved is False:
            return (
                PostureState.ARMED_DEFERRED,
                "buy_intent_not_approved",
                distance_pct,
            )
        return PostureState.RESTING, "approved_buy_support", distance_pct

    raise AssertionError("unmapped level role reached classifier")


def generate_posture(
    snapshot: PostureGeneratorInput,
    *,
    posture_policy: PosturePolicy,
    policy_version: str,
    policy_content_hash: str,
    generated_at: dt.datetime | None = None,
) -> PostureGenerationResult:
    """Generate a declarative five-state posture without side effects."""

    if not posture_policy.enabled:
        raise PostureDisabledError("posture.enabled=false")
    if posture_policy.mode != "shadow":
        raise ValueError("ROB-1106 generator supports shadow mode only")
    if not posture_policy.policy_stamp_required:
        raise ValueError("policy stamp is required")
    if not policy_version.strip() or not policy_content_hash.strip():
        raise ValueError("non-empty policy version/content hash are required")
    if set(posture_policy.states) != {state.value for state in POSTURE_STATES}:
        raise ValueError("policy state machine must contain exactly five states")

    quotes = {(row.market, row.symbol.upper()): row for row in snapshot.quotes}
    contexts = {row.holding_id: row for row in snapshot.policy_contexts}
    assignments: list[PostureAssignment] = []
    unmapped: list[UnmappedHolding] = []

    for holding in snapshot.holdings:
        context = contexts.get(holding.holding_id)
        gap = _unmapped_reason(holding=holding, context=context)
        if gap is not None:
            unmapped.append(
                UnmappedHolding(
                    holding_id=holding.holding_id,
                    account_id=holding.account_id,
                    symbol=holding.symbol,
                    market=holding.market,
                    reason=gap,
                    level_role=context.level_role if context is not None else None,
                )
            )
            continue

        assert context is not None
        quote = quotes.get((holding.market, holding.symbol.upper()))
        state, reason, distance_pct = _classify(
            holding=holding,
            quote=quote,
            context=context,
        )
        assignments.append(
            PostureAssignment(
                holding_id=holding.holding_id,
                account_id=holding.account_id,
                symbol=holding.symbol,
                market=holding.market,
                state=state,
                reason=reason,
                intent=context.intent,
                level_role=context.level_role,
                quantity=holding.quantity,
                current_price=quote.price if quote is not None else None,
                level_price=context.level_price,
                target_distance_pct=distance_pct,
                review_at=context.review_at,
                policy_version=policy_version,
                policy_content_hash=policy_content_hash,
            )
        )

    state_counts = {
        state: sum(row.state is state for row in assignments)
        for state in POSTURE_STATES
    }
    state_symbol_counts = {
        state: len(
            {
                (row.market, row.symbol.upper())
                for row in assignments
                if row.state is state
            }
        )
        for state in POSTURE_STATES
    }
    input_count = len(snapshot.holdings)
    mapped_count = len(assignments)
    coverage = PostureCoverage(
        input_holding_count=input_count,
        mapped_holding_count=mapped_count,
        unmapped_holding_count=len(unmapped),
        coverage_ratio=(mapped_count / input_count) if input_count else 1.0,
        state_counts=state_counts,
        state_symbol_counts=state_symbol_counts,
    )
    return PostureGenerationResult(
        captured_at=snapshot.captured_at,
        generated_at=generated_at or dt.datetime.now(dt.UTC),
        policy_version=policy_version,
        policy_content_hash=policy_content_hash,
        assignments=assignments,
        unmapped_holdings=unmapped,
        coverage=coverage,
    )
