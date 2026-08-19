"""Multi-axis funding-route comparison with no hidden scalar score."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.schemas.funding_advisory import canonical_decimal
from app.services.funding_advisory.contracts import FundingRoute

_REVERSIBILITY_RANK = {
    "reversible": 0,
    "conditional": 1,
    "irreversible": 2,
    "unknown": 3,
}


def _comparable(route: FundingRoute) -> bool:
    return (
        route.amount_status == "known"
        and route.route_fundable_amount is not None
        and route.route_fundable_amount > 0
        and route.deadline_status == "met"
        and route.explicit_cost is not None
        and route.eta_minutes is not None
        and route.realized_impact is not None
        and route.reversibility != "unknown"
        and route.eligibility == "eligible"
    )


def dominates(left: FundingRoute, right: FundingRoute) -> bool:
    """True when left is no worse on every disclosed axis and better on one."""

    if not _comparable(left) or not _comparable(right):
        return False
    left_axes = (
        -Decimal(left.route_fundable_amount or 0),
        Decimal(left.explicit_cost or 0),
        Decimal(left.eta_minutes or 0),
        abs(Decimal(left.realized_impact or 0)),
        Decimal(_REVERSIBILITY_RANK[left.reversibility]),
    )
    right_axes = (
        -Decimal(right.route_fundable_amount or 0),
        Decimal(right.explicit_cost or 0),
        Decimal(right.eta_minutes or 0),
        abs(Decimal(right.realized_impact or 0)),
        Decimal(_REVERSIBILITY_RANK[right.reversibility]),
    )
    return all(a <= b for a, b in zip(left_axes, right_axes, strict=True)) and any(
        a < b for a, b in zip(left_axes, right_axes, strict=True)
    )


def compare_routes(routes: list[FundingRoute]) -> list[FundingRoute]:
    comparable = [route for route in routes if _comparable(route)]
    result: list[FundingRoute] = []
    for route in routes:
        if not _comparable(route):
            result.append(route.model_copy(update={"comparison": "unavailable"}))
            continue
        if any(dominates(other, route) for other in comparable if other is not route):
            comparison = "dominated"
        elif len(comparable) > 1 and all(
            dominates(route, other) for other in comparable if other is not route
        ):
            comparison = "preferred"
        else:
            comparison = "situation_dependent"
        result.append(route.model_copy(update={"comparison": comparison}))
    return result


def build_reference_combination(
    routes: list[FundingRoute], *, shortfall: Decimal
) -> dict[str, Any]:
    """Return a disclosed-cost reference only; it is never auto-selected."""

    candidates = [route for route in compare_routes(routes) if _comparable(route)]
    candidates.sort(
        key=lambda route: (
            route.comparison == "dominated",
            Decimal(route.explicit_cost or 0),
            int(route.eta_minutes or 0),
            route.route_id,
        )
    )
    remaining = shortfall
    cumulative = Decimal("0")
    legs: list[dict[str, str]] = []
    for route in candidates:
        if remaining <= 0:
            break
        amount = min(Decimal(route.route_fundable_amount or 0), remaining)
        cumulative += amount
        remaining -= amount
        legs.append(
            {
                "route_id": route.route_id,
                "planned_amount": canonical_decimal(amount),
                "cumulative_planned_amount": canonical_decimal(cumulative),
                "remaining_gap": canonical_decimal(max(remaining, Decimal("0"))),
            }
        )
    return {
        "selected": False,
        "scenario_kind": "reference_only",
        "selection_basis": "operator_decision_required_multi_axis",
        "legs": legs,
        "remaining_gap": canonical_decimal(max(remaining, Decimal("0"))),
    }


__all__ = ["build_reference_combination", "compare_routes", "dominates"]
