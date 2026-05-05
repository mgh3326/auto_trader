"""ROB-116 — Pure-function classifier for portfolio action candidates.

Side-effect free; aggregation lives in PortfolioActionService.
"""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict

CandidateAction = Literal["sell", "trim", "hold", "add", "watch"]

OVERWEIGHT_PCT = 25.0
UNDERWEIGHT_PCT = 5.0
NEAR_LEVEL_PCT = 1.5
LOSS_THRESHOLD_PCT = -10.0


class ClassifierInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    position_weight_pct: float | None
    profit_rate: float | None
    summary_decision: Literal["buy", "hold", "sell"] | None
    summary_confidence: int | None = None
    market_verdict: Literal["bull", "bear", "neutral", "unavailable"] | None = None
    nearest_support_pct: float | None
    nearest_resistance_pct: float | None
    journal_status: Literal["present", "missing", "stale"]
    sellable_quantity: float | None
    staked_quantity: float | None


@dataclass(frozen=True)
class ClassifierResult:
    candidate_action: CandidateAction
    suggested_trim_pct: int | None
    reason_codes: list[str]
    missing_context_codes: list[str]


def classify_position(inputs: ClassifierInputs) -> ClassifierResult:
    reasons: list[str] = []
    missing: list[str] = []

    weight = inputs.position_weight_pct or 0.0
    decision = inputs.summary_decision

    if weight >= OVERWEIGHT_PCT:
        reasons.append("overweight")
    elif weight <= UNDERWEIGHT_PCT:
        reasons.append("underweight")

    if decision == "buy":
        reasons.append("research_bullish")
    elif decision == "sell":
        reasons.append("research_bearish")
    elif decision == "hold":
        reasons.append("research_not_bullish")
    else:
        reasons.append("research_missing")

    if (
        inputs.nearest_resistance_pct is not None
        and inputs.nearest_resistance_pct <= NEAR_LEVEL_PCT
    ):
        reasons.append("near_resistance")
    if (
        inputs.nearest_support_pct is not None
        and inputs.nearest_support_pct >= -NEAR_LEVEL_PCT
    ):
        reasons.append("near_support")

    if inputs.journal_status != "present":
        missing.append("journal_missing")
    if inputs.staked_quantity is None and inputs.sellable_quantity is None:
        missing.append("staked_quantity_unknown")

    action: CandidateAction
    suggested_trim_pct: int | None = None

    if decision == "sell":
        action = "sell"
    elif decision == "buy" and weight < OVERWEIGHT_PCT:
        action = "add"
    elif weight >= OVERWEIGHT_PCT and decision != "buy":
        action = "trim"
        suggested_trim_pct = 20
    elif (
        inputs.profit_rate is not None
        and inputs.profit_rate <= LOSS_THRESHOLD_PCT
        and decision != "buy"
    ):
        action = "trim"
        suggested_trim_pct = 25
    elif decision == "hold":
        action = "hold"
    else:
        action = "watch"

    return ClassifierResult(
        candidate_action=action,
        suggested_trim_pct=suggested_trim_pct,
        reason_codes=reasons,
        missing_context_codes=missing,
    )
