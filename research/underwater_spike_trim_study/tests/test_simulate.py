"""Algebra of the three options, including the cost-basis invariance claim."""

from __future__ import annotations

import pytest

from research.underwater_spike_trim_study.simulate import (
    cost_basis_view,
    deltas_vs_hold,
    normalised,
    option_values,
)
from research.underwater_spike_trim_study.spec import COST_BASIS_GRID, TRIM_FRACTION


def test_hold_is_the_exit_price():
    values = option_values(p0=100.0, pt=90.0, rebuy_price=None, window_low=80.0)
    assert values.hold == 90.0
    assert normalised(values, 100.0)["hold"] == pytest.approx(-0.10)


def test_trim_keeps_the_trimmed_tenth_at_the_event_price():
    values = option_values(p0=100.0, pt=90.0, rebuy_price=None, window_low=95.0)
    assert values.trim == pytest.approx(0.9 * 90.0 + 0.1 * 100.0)
    assert deltas_vs_hold(values, 100.0)["trim"] == pytest.approx(
        TRIM_FRACTION * (100.0 - 90.0) / 100.0
    )


def test_trim_loses_to_hold_when_price_rises():
    values = option_values(p0=100.0, pt=130.0, rebuy_price=None, window_low=99.0)
    assert deltas_vs_hold(values, 100.0)["trim"] < 0


def test_rebid_unavailable_is_recorded_as_none_not_as_trim():
    values = option_values(p0=100.0, pt=90.0, rebuy_price=None, window_low=50.0)
    assert values.rebid is None
    assert deltas_vs_hold(values, 100.0)["rebid"] is None


def test_rebid_unfilled_equals_trim():
    values = option_values(p0=100.0, pt=90.0, rebuy_price=80.0, window_low=85.0)
    assert values.rebuy_filled is False
    assert values.rebid == pytest.approx(values.trim)


def test_rebid_filled_beats_hold_by_the_discount_on_the_trimmed_tenth():
    values = option_values(p0=100.0, pt=90.0, rebuy_price=80.0, window_low=79.0)
    assert values.rebuy_filled is True
    assert values.rebid == pytest.approx(90.0 + TRIM_FRACTION * (100.0 - 80.0))
    assert deltas_vs_hold(values, 100.0)["rebid"] == pytest.approx(
        TRIM_FRACTION * (100.0 - 80.0) / 100.0
    )


def test_rebid_above_the_decision_price_is_rejected():
    with pytest.raises(ValueError):
        option_values(p0=100.0, pt=90.0, rebuy_price=100.0, window_low=90.0)


@pytest.mark.parametrize("pt", [55.0, 90.0, 100.0, 143.0])
def test_cost_basis_does_not_change_ranking(pt: float):
    """🔴 The +10/+20/+30% grid cannot reorder the options.

    An underwater average cost enters every option's P&L as the same additive
    constant, so it cancels in every pairwise difference.  The report states
    this rather than presenting a sensitivity that does not exist.
    """
    values = option_values(p0=100.0, pt=pt, rebuy_price=85.0, window_low=80.0)
    baseline = deltas_vs_hold(values, 100.0)
    orderings = set()
    for premium in COST_BASIS_GRID:
        view = cost_basis_view(values, p0=100.0, cost_premium=premium)
        ranked = tuple(
            sorted(
                ("hold", "trim", "rebid"),
                key=lambda name: view[f"{name}_pnl_vs_cost"],
                reverse=True,
            )
        )
        orderings.add(ranked)
        # differences in cost-basis space equal differences in p0 space
        assert view["trim_pnl_vs_cost"] - view["hold_pnl_vs_cost"] == pytest.approx(
            baseline["trim"] / (1 + premium)
        )
    assert len(orderings) == 1


def test_realised_loss_on_trim_is_negative_and_grows_with_the_premium():
    values = option_values(p0=100.0, pt=100.0, rebuy_price=None, window_low=100.0)
    losses = [
        cost_basis_view(values, p0=100.0, cost_premium=premium)[
            "realised_loss_on_trim_pct_of_cost"
        ]
        for premium in COST_BASIS_GRID
    ]
    assert all(loss < 0 for loss in losses)
    assert losses == sorted(losses, reverse=True)
