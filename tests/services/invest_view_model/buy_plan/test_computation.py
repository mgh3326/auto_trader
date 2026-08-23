"""§144차 — the 매수 계획 board's arithmetic, pinned.

These are the numbers an operator moves money against, so the turn point, the
sampled cash curve, and the approval-lane classification are all fixed here
rather than left to whatever the aggregate happens to emit.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.invest_view_model.buy_plan.computation import (
    approval_lane_for,
    averaging_additional_notional,
    averaging_turn_point,
    turn_point_price,
)


@pytest.mark.unit
def test_turn_point_is_average_over_one_plus_k() -> None:
    # k=0.10 → an add only becomes valid once the price is ~9.09% under the
    # average. This is the §136차 note made executable: raising k pushes the
    # turn point *down*, which is why k was not raised.
    assert turn_point_price(
        average_price=Decimal("1100"), k=Decimal("0.10")
    ) == Decimal("1000")


@pytest.mark.unit
def test_a_of_k_is_zero_exactly_at_the_turn_point() -> None:
    """``a_limit_lte_zero: NO_ORDER`` — the boundary itself buys nothing."""

    average = Decimal("1100")
    star = turn_point_price(average_price=average, k=Decimal("0.10"))
    assert (
        averaging_additional_notional(
            cost_basis=Decimal("1100000"),
            average_price=average,
            price=star,
            k=Decimal("0.10"),
        )
        == 0
    )


@pytest.mark.unit
def test_a_of_k_is_clamped_at_zero_above_the_turn_point() -> None:
    """A raw negative must not surface as a negative "credit"."""

    assert (
        averaging_additional_notional(
            cost_basis=Decimal("1000000"),
            average_price=Decimal("1000"),
            price=Decimal("1200"),
            k=Decimal("0.10"),
        )
        == 0
    )


@pytest.mark.unit
def test_a_of_k_actually_pulls_the_average_into_the_k_band() -> None:
    """The formula's defining property, checked by re-deriving the new average."""

    cost_basis = Decimal("1000000")
    average = Decimal("1000")
    quantity = cost_basis / average
    k = Decimal("0.10")
    price = Decimal("800")

    added = averaging_additional_notional(
        cost_basis=cost_basis, average_price=average, price=price, k=k
    )
    assert added > 0

    new_average = (cost_basis + added) / (quantity + added / price)
    assert new_average == pytest.approx(float(price * (1 + k)), rel=Decimal("1e-12"))


@pytest.mark.unit
def test_turn_point_projection_samples_below_the_boundary() -> None:
    projection = averaging_turn_point(
        cost_basis=Decimal("1000000"),
        average_price=Decimal("1100"),
        current_price=Decimal("1050"),
        k=Decimal("0.10"),
    )

    assert projection.turn_point_price == Decimal("1000")
    # 1050 is still 5% above the turn point → NO_ORDER today.
    assert projection.distance_to_turn_point_pct == Decimal("5")
    assert projection.reached is False

    offsets = [s.offset_from_turn_point_pct for s in projection.samples]
    assert offsets == [Decimal("-1"), Decimal("-3")]
    # Deeper price ⇒ strictly more cash required.
    assert projection.samples[1].additional_notional > (
        projection.samples[0].additional_notional
    )
    assert all(sample.additional_notional > 0 for sample in projection.samples)


@pytest.mark.unit
def test_turn_point_reached_requires_strictly_below() -> None:
    at_boundary = averaging_turn_point(
        cost_basis=Decimal("1000000"),
        average_price=Decimal("1100"),
        current_price=Decimal("1000"),
        k=Decimal("0.10"),
    )
    assert at_boundary.reached is False

    below = averaging_turn_point(
        cost_basis=Decimal("1000000"),
        average_price=Decimal("1100"),
        current_price=Decimal("999"),
        k=Decimal("0.10"),
    )
    assert below.reached is True
    assert below.distance_to_turn_point_pct < 0


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad",
    [
        {"cost_basis": Decimal("0")},
        {"average_price": Decimal("0")},
        {"current_price": Decimal("-1")},
        {"k": Decimal("0")},
    ],
)
def test_turn_point_rejects_non_positive_inputs(bad: dict) -> None:
    kwargs = {
        "cost_basis": Decimal("1000"),
        "average_price": Decimal("100"),
        "current_price": Decimal("90"),
        "k": Decimal("0.10"),
    }
    kwargs.update(bad)
    with pytest.raises(ValueError):
        averaging_turn_point(**kwargs)


@pytest.mark.unit
def test_approval_lane_auto_submits_within_the_tier_ceiling() -> None:
    lane, reason = approval_lane_for(
        notional=Decimal("150000"),
        tier_auto_submit_notional=Decimal("200000"),
        per_order_auto_approve_cap=Decimal("1000000"),
    )
    assert (lane, reason) == ("auto_submit", "within_tier_auto_submit_notional")


@pytest.mark.unit
def test_approval_lane_cards_above_the_tier_ceiling() -> None:
    lane, reason = approval_lane_for(
        notional=Decimal("250000"),
        tier_auto_submit_notional=Decimal("200000"),
        per_order_auto_approve_cap=Decimal("1000000"),
    )
    assert (lane, reason) == ("human_card", "above_tier_auto_submit_notional")


@pytest.mark.unit
def test_approval_lane_reports_the_per_order_cap_first() -> None:
    """The cap is the harder boundary, so it owns the reason string."""

    lane, reason = approval_lane_for(
        notional=Decimal("1500000"),
        tier_auto_submit_notional=Decimal("200000"),
        per_order_auto_approve_cap=Decimal("1000000"),
    )
    assert (lane, reason) == ("human_card", "above_per_order_auto_approve_cap")


@pytest.mark.unit
@pytest.mark.parametrize(
    "notional,ceiling,cap",
    [
        (None, Decimal("200000"), Decimal("1000000")),
        (Decimal("0"), Decimal("200000"), Decimal("1000000")),
        (Decimal("100"), None, Decimal("1000000")),
        (Decimal("100"), Decimal("200000"), None),
    ],
)
def test_approval_lane_fails_closed_when_a_bound_is_unknown(
    notional: Decimal | None, ceiling: Decimal | None, cap: Decimal | None
) -> None:
    """Never show 자동승인 for something the board could not actually check."""

    lane, _ = approval_lane_for(
        notional=notional,
        tier_auto_submit_notional=ceiling,
        per_order_auto_approve_cap=cap,
    )
    assert lane == "human_card"


@pytest.mark.unit
def test_matches_the_policy_table_implementation() -> None:
    """Anti-drift: app/ and scripts/ must agree on A(k) to the last digit."""

    from scripts.policy_table.core.averaging import averaging_math

    cases = [
        (Decimal("1000000"), Decimal("1000"), Decimal("800")),
        (Decimal("3210000"), Decimal("1234.5"), Decimal("1000.25")),
        (Decimal("500000"), Decimal("100"), Decimal("120")),
    ]
    for cost_basis, average_price, price in cases:
        reference = averaging_math(
            cost_basis=cost_basis,
            average_price=average_price,
            current_price=price,
            k=Decimal("0.10"),
        )
        assert (
            averaging_additional_notional(
                cost_basis=cost_basis,
                average_price=average_price,
                price=price,
                k=Decimal("0.10"),
            )
            == reference["additional_notional"]
        )
