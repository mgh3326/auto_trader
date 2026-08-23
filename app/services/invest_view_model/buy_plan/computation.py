"""Pure arithmetic behind the /invest 매수 계획 board (§144차).

Everything in this module is a **display approximation of the policy
formula**, not a reproduction of a session's verdict. A session weighs support
quality, R-931 review state, sector concentration, crash-day state and much
else that this module deliberately does not model; the numbers here answer one
narrower question the operator asked — *"이 트리거가 걸리면 돈이 얼마나
필요한가"* — so cash can be moved in advance.

No database, broker, network, clock, or environment access lives here.

Averaging-down (물타기) arithmetic
---------------------------------
``config/trading_policy.yaml`` → ``decision_rules['buy.support_reserve_net']
.add_candidate`` fixes ``k_used`` and ``a_limit_lte_zero: NO_ORDER``. The
notional that pulls a position's average cost down to within ``k`` of the fill
price ``p`` is

    A(p) = C · (1 − (p/P)·(1+k)) / k

with ``C`` the cost basis and ``P`` the current average price — the same
formula as ``scripts/policy_table/core/averaging.py``; a parity test pins the
two implementations together so they cannot drift.

A(p) is positive only while ``p < P/(1+k)``. That boundary is the **turn
point** ``P*``: above it the policy says NO_ORDER, below it an add becomes
arithmetically meaningful and grows as price falls. ``P*`` is what the board
shows as the trigger price, and the requested cash curve is sampled at fixed
offsets below it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final, Literal

# Mirrors research.kr_corpus.d3_engine.constants, which
# scripts/policy_table/core/averaging.py runs under. Restated rather than
# imported so app/ does not depend on research/ or scripts/; the parity test
# proves the two agree.
DECIMAL_PRECISION: Final = 50

APPROXIMATION_NOTICE: Final = (
    "이 보드의 수치는 정책 산식의 표시용 근사입니다. 판정 정본은 회차이며, "
    "여기서 계산된 금액·트리거가 주문을 만들거나 승인하지 않습니다."
)

# Sampled offsets below the turn point. The turn point itself is where
# A(p) == 0, so it is useless as a cash estimate — the board reports the two
# representative depths the operator asked for instead.
TURN_POINT_SAMPLE_OFFSETS_PCT: Final = (Decimal("-1"), Decimal("-3"))

ApprovalLane = Literal["auto_submit", "human_card"]

# Closed vocabulary. A new reason must be added here rather than passed
# through as free text, so the frontend can never render an unlabelled lane.
ApprovalLaneReason = Literal[
    "within_tier_auto_submit_notional",
    "above_tier_auto_submit_notional",
    "above_per_order_auto_approve_cap",
    "notional_unavailable",
]


@dataclass(frozen=True, slots=True)
class AveragingSample:
    """One point on the A(k) cash curve below the turn point."""

    offset_from_turn_point_pct: Decimal
    price: Decimal
    additional_notional: Decimal
    target_average_price: Decimal


@dataclass(frozen=True, slots=True)
class AveragingTurnPoint:
    """Where an underwater lot becomes an arithmetically valid A(k) add."""

    k: Decimal
    average_price: Decimal
    cost_basis: Decimal
    current_price: Decimal
    turn_point_price: Decimal
    # Signed: positive = price still above the turn point (NO_ORDER today),
    # negative = already past it. Percent of the turn point price.
    distance_to_turn_point_pct: Decimal
    reached: bool
    samples: tuple[AveragingSample, ...]


def averaging_additional_notional(
    *,
    cost_basis: Decimal,
    average_price: Decimal,
    price: Decimal,
    k: Decimal,
) -> Decimal:
    """A(p) clamped at zero — the policy's ``a_limit_lte_zero: NO_ORDER``.

    A non-positive raw value means the average is already within ``k`` of
    ``price``; the tier then places no order at all, so zero is the honest
    projection rather than a negative "credit".
    """

    if cost_basis <= 0 or average_price <= 0 or price <= 0:
        raise ValueError("cost_basis, average_price and price must be positive")
    if k <= 0:
        raise ValueError("k must be positive")

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        ratio = price / average_price
        raw = cost_basis * (Decimal(1) - ratio * (Decimal(1) + k)) / k

    return raw if raw > 0 else Decimal(0)


def turn_point_price(*, average_price: Decimal, k: Decimal) -> Decimal:
    """``P* = P / (1 + k)`` — the price where A(k) crosses zero."""

    if average_price <= 0:
        raise ValueError("average_price must be positive")
    if k <= 0:
        raise ValueError("k must be positive")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return average_price / (Decimal(1) + k)


def averaging_turn_point(
    *,
    cost_basis: Decimal,
    average_price: Decimal,
    current_price: Decimal,
    k: Decimal,
    sample_offsets_pct: tuple[Decimal, ...] = TURN_POINT_SAMPLE_OFFSETS_PCT,
) -> AveragingTurnPoint:
    """Full turn-point projection for one underwater lot."""

    if cost_basis <= 0 or average_price <= 0 or current_price <= 0:
        raise ValueError("cost_basis, average_price and current_price must be positive")
    if k <= 0:
        raise ValueError("k must be positive")

    p_star = turn_point_price(average_price=average_price, k=k)

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        distance_pct = (current_price - p_star) / p_star * Decimal(100)
        samples = tuple(
            _sample(
                cost_basis=cost_basis,
                average_price=average_price,
                k=k,
                turn_point=p_star,
                offset_pct=offset,
            )
            for offset in sample_offsets_pct
        )

    return AveragingTurnPoint(
        k=k,
        average_price=average_price,
        cost_basis=cost_basis,
        current_price=current_price,
        turn_point_price=p_star,
        distance_to_turn_point_pct=distance_pct,
        # Exactly at the turn point A(k) == 0, which the policy calls
        # NO_ORDER — so "reached" requires strictly below.
        reached=current_price < p_star,
        samples=samples,
    )


def _sample(
    *,
    cost_basis: Decimal,
    average_price: Decimal,
    k: Decimal,
    turn_point: Decimal,
    offset_pct: Decimal,
) -> AveragingSample:
    price = turn_point * (Decimal(1) + offset_pct / Decimal(100))
    return AveragingSample(
        offset_from_turn_point_pct=offset_pct,
        price=price,
        additional_notional=averaging_additional_notional(
            cost_basis=cost_basis,
            average_price=average_price,
            price=price,
            k=k,
        ),
        target_average_price=price * (Decimal(1) + k),
    )


def approval_lane_for(
    *,
    notional: Decimal | None,
    tier_auto_submit_notional: Decimal | None,
    per_order_auto_approve_cap: Decimal | None,
) -> tuple[ApprovalLane, ApprovalLaneReason]:
    """Classify a projected order into 자동승인 vs 카드.

    Fail-closed in both directions: an unknown notional, an unknown tier
    ceiling, or an unknown per-order cap all resolve to ``human_card``. The
    board must never show 자동승인 for something it could not actually check.
    """

    if notional is None or notional <= 0:
        return "human_card", "notional_unavailable"
    if per_order_auto_approve_cap is None or tier_auto_submit_notional is None:
        return "human_card", "notional_unavailable"
    if notional > per_order_auto_approve_cap:
        return "human_card", "above_per_order_auto_approve_cap"
    if notional > tier_auto_submit_notional:
        return "human_card", "above_tier_auto_submit_notional"
    return "auto_submit", "within_tier_auto_submit_notional"
