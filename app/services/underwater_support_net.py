"""§176차 — the ``buy.underwater_support_net`` eligibility formula, made countable.

The policy states the tier as a session contract; this module is the same
statement in a form that can be evaluated and disagreed with. It is PURE:
no database, no broker, no clock, and no order path imports it. It creates
nothing, submits nothing, and blocks nothing — a caller receives a verdict and
a set of numbers, and every decision about what to do with them stays where it
already was.

The five clauses, in the order the §0 classification formula states them:

1. HELD, and the *account lot* is at a loss. Average cost is per account, so a
   KIS lot is only averaged down by a KIS buy.
2. Unrealized P&L at or below -8% (inclusive).
3. Support of at least ``moderate`` strength, confirmed by at least two
   independent source families, sitting between -12% and -3% of the market.
4. Thesis-alive evidence at decision time. Absence of evidence is not
   evidence: a lot with no surviving thesis is a ``sell.loss_cut`` candidate,
   not an averaging-down one.
5. After the 50% size cap is applied, the required rebound improves by at
   least 3 percentage points. This is the clause that rejects a token add
   which spends cash and moves the escape price by nothing.

"Required rebound" is the rise from the current price to the point where the
existing fail-closed loss-sell guard would let the lot out:
``average_cost x sell.loss_guard_min_multiple / current_price - 1``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal
from typing import Any

from app.services.trading_policy_service import (
    load_trading_policy,
    policy_version_stamp,
)

UNDERWATER_RULE_KEY = "buy.underwater_support_net"
UNDERWATER_TIER_ID = "underwater_support_net"

_STRENGTH_RANK = {"weak": 0, "moderate": 1, "strong": 2}

# Closed rejection vocabulary. A caller that does not recognise a reason must
# treat the candidate as ineligible, never as eligible.
REASON_NOT_HELD = "not_held"
REASON_NOT_UNDERWATER = "position_not_underwater"
REASON_LOSS_ABOVE_FLOOR = "loss_above_tier_floor"
REASON_SUPPORT_STRENGTH = "support_strength_below_minimum"
REASON_SUPPORT_FAMILIES = "independent_support_family_count_below_minimum"
REASON_SUPPORT_BAND = "support_distance_outside_band"
REASON_THESIS = "thesis_alive_evidence_missing"
REASON_SIZING = "size_cap_admits_no_whole_share"
REASON_IMPROVEMENT = "required_rebound_improvement_below_minimum"
REASON_INPUT = "input_unusable"


class UnderwaterPolicyError(ValueError):
    """The policy does not carry a usable ``buy.underwater_support_net`` tier."""


@dataclass(frozen=True, slots=True)
class UnderwaterTierPolicy:
    unrealized_pnl_pct_max_inclusive: Decimal
    support_strength_min: str
    independent_support_source_count_min: int
    support_distance_pct_range: tuple[Decimal, Decimal]
    required_rebound_improvement_pct_min: Decimal
    max_add_notional_pct_of_position: Decimal
    loss_guard_multiple: Decimal


@dataclass(frozen=True, slots=True)
class UnderwaterLot:
    """One account lot plus the support level being considered for it."""

    symbol: str
    market: str
    quantity: Decimal
    average_cost: Decimal
    current_price: Decimal
    support_price: Decimal
    support_strength: str
    support_source_families: tuple[str, ...]
    thesis_alive: bool
    rung_price: Decimal


def _required_key(conditions: Any, key: str) -> Any:
    try:
        return conditions[key]
    except (KeyError, TypeError) as exc:
        raise UnderwaterPolicyError(
            f"{UNDERWATER_RULE_KEY} is missing condition {key!r}"
        ) from exc


def load_underwater_tier_policy(policy: Any | None = None) -> UnderwaterTierPolicy:
    """Read the tier's literals, failing closed rather than assuming defaults."""

    document = policy if policy is not None else load_trading_policy()
    try:
        rule = document.decision_rules[UNDERWATER_RULE_KEY]
    except KeyError as exc:
        raise UnderwaterPolicyError(
            f"{UNDERWATER_RULE_KEY} is missing from the trading policy"
        ) from exc
    tiers = [
        tier for tier in getattr(rule, "tiers", []) if tier.id == UNDERWATER_TIER_ID
    ]
    if len(tiers) != 1:
        raise UnderwaterPolicyError(
            f"{UNDERWATER_RULE_KEY} must declare exactly one {UNDERWATER_TIER_ID} tier"
        )
    conditions = tiers[0].conditions
    band = _required_key(conditions, "support_distance_from_current_pct_range")
    if not isinstance(band, list) or len(band) != 2:
        raise UnderwaterPolicyError(
            f"{UNDERWATER_RULE_KEY} support band must be two numbers"
        )
    strength = str(_required_key(conditions, "support_strength_min"))
    if strength not in _STRENGTH_RANK:
        raise UnderwaterPolicyError(
            f"{UNDERWATER_RULE_KEY} support_strength_min {strength!r} is not a "
            "known strength"
        )
    # The escape price is the existing loss guard's, read from its own key
    # rather than restated here, so the two can never disagree.
    guard = document.thresholds["sell.loss_guard_min_multiple"].value
    return UnderwaterTierPolicy(
        unrealized_pnl_pct_max_inclusive=Decimal(
            str(_required_key(conditions, "unrealized_pnl_pct_max_inclusive"))
        ),
        support_strength_min=strength,
        independent_support_source_count_min=int(
            _required_key(conditions, "independent_support_source_count_min")
        ),
        support_distance_pct_range=(Decimal(str(band[0])), Decimal(str(band[1]))),
        required_rebound_improvement_pct_min=Decimal(
            str(_required_key(conditions, "required_rebound_improvement_pct_min"))
        ),
        max_add_notional_pct_of_position=Decimal(
            str(_required_key(conditions, "max_add_notional_pct_of_position"))
        ),
        loss_guard_multiple=Decimal(str(guard)),
    )


def required_rebound_pct(
    *,
    average_cost: Decimal,
    current_price: Decimal,
    loss_guard_multiple: Decimal,
) -> Decimal:
    """Rise needed from here to clear the fail-closed loss-sell guard."""

    return (
        average_cost * loss_guard_multiple / current_price - Decimal("1")
    ) * Decimal("100")


def _pct(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")), "f")


def _amount(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.0001")).normalize(), "f")


def evaluate_underwater_support_net(
    lot: UnderwaterLot,
    *,
    policy: Any | None = None,
    tier_policy: UnderwaterTierPolicy | None = None,
) -> dict[str, Any]:
    """Judge one account lot against the tier and size the add it permits.

    Returns a verdict dict; ``eligible`` is only ever ``True`` when EVERY
    clause passed. ``reasons`` lists every clause that failed, not just the
    first, so a caller can see the whole picture rather than re-running the
    evaluation after each fix.
    """

    stamp = policy_version_stamp()
    result: dict[str, Any] = {
        "policy_key": UNDERWATER_RULE_KEY,
        "policy_version": stamp["version"],
        "policy_content_hash": stamp["content_hash"],
        "symbol": lot.symbol,
        "market": lot.market,
        "eligible": False,
        "reasons": [],
    }
    contract = tier_policy or load_underwater_tier_policy(policy)

    reasons: list[str] = []
    if lot.quantity <= 0 or lot.average_cost <= 0 or lot.current_price <= 0:
        result["reasons"] = [REASON_INPUT if lot.quantity > 0 else REASON_NOT_HELD]
        return result

    position_notional = lot.quantity * lot.current_price
    pnl_pct = (lot.current_price / lot.average_cost - Decimal("1")) * Decimal("100")
    result["unrealized_pnl_pct"] = _pct(pnl_pct)
    result["position_notional"] = _amount(position_notional)

    # Clause 1/2 — held and underwater, at or past the floor. The comparison is
    # INCLUSIVE: a lot at exactly -8.00% qualifies, which is what "손실 <= -8%"
    # says.
    if pnl_pct >= 0:
        reasons.append(REASON_NOT_UNDERWATER)
    elif pnl_pct > contract.unrealized_pnl_pct_max_inclusive:
        reasons.append(REASON_LOSS_ABOVE_FLOOR)

    # Clause 3 — support quality and distance.
    strength_rank = _STRENGTH_RANK.get(str(lot.support_strength).strip().lower(), -1)
    if strength_rank < _STRENGTH_RANK[contract.support_strength_min]:
        reasons.append(REASON_SUPPORT_STRENGTH)
    families = {family for family in lot.support_source_families if family}
    if len(families) < contract.independent_support_source_count_min:
        reasons.append(REASON_SUPPORT_FAMILIES)
    support_distance_pct = (
        lot.support_price / lot.current_price - Decimal("1")
    ) * Decimal("100")
    result["support_distance_pct"] = _pct(support_distance_pct)
    low, high = contract.support_distance_pct_range
    if not (low <= support_distance_pct <= high):
        reasons.append(REASON_SUPPORT_BAND)

    # Clause 4 — thesis.
    if not lot.thesis_alive:
        reasons.append(REASON_THESIS)

    # Clause 5 — sizing, then the improvement the sizing actually buys.
    max_add_notional = (
        position_notional * contract.max_add_notional_pct_of_position / Decimal("100")
    )
    result["max_add_notional"] = _amount(max_add_notional)
    add_quantity = Decimal("0")
    if lot.rung_price > 0:
        add_quantity = (max_add_notional / lot.rung_price).to_integral_value(
            rounding=ROUND_FLOOR
        )
    result["rung_price"] = _amount(lot.rung_price)
    result["add_quantity"] = _amount(add_quantity)

    before = required_rebound_pct(
        average_cost=lot.average_cost,
        current_price=lot.current_price,
        loss_guard_multiple=contract.loss_guard_multiple,
    )
    result["required_rebound_pct_before"] = _pct(before)

    if add_quantity <= 0:
        reasons.append(REASON_SIZING)
        result["reasons"] = sorted(set(reasons))
        return result

    add_notional = add_quantity * lot.rung_price
    new_average_cost = (lot.quantity * lot.average_cost + add_notional) / (
        lot.quantity + add_quantity
    )
    after = required_rebound_pct(
        average_cost=new_average_cost,
        current_price=lot.current_price,
        loss_guard_multiple=contract.loss_guard_multiple,
    )
    improvement = before - after
    result["add_notional"] = _amount(add_notional)
    result["new_average_cost"] = _amount(new_average_cost)
    result["required_rebound_pct_after"] = _pct(after)
    result["required_rebound_improvement_pct_points"] = _pct(improvement)
    if improvement < contract.required_rebound_improvement_pct_min:
        reasons.append(REASON_IMPROVEMENT)

    result["reasons"] = sorted(set(reasons))
    result["eligible"] = not reasons
    return result


__all__ = [
    "REASON_IMPROVEMENT",
    "REASON_INPUT",
    "REASON_LOSS_ABOVE_FLOOR",
    "REASON_NOT_HELD",
    "REASON_NOT_UNDERWATER",
    "REASON_SIZING",
    "REASON_SUPPORT_BAND",
    "REASON_SUPPORT_FAMILIES",
    "REASON_SUPPORT_STRENGTH",
    "REASON_THESIS",
    "UNDERWATER_RULE_KEY",
    "UNDERWATER_TIER_ID",
    "UnderwaterLot",
    "UnderwaterPolicyError",
    "UnderwaterTierPolicy",
    "evaluate_underwater_support_net",
    "load_underwater_tier_policy",
    "required_rebound_pct",
]
