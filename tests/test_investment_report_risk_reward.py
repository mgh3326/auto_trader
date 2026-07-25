"""ROB-690 — pure risk/reward (R:R) arithmetic unit tests.

Deterministic arithmetic only: no DB/session fixtures needed.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.services.investment_reports.risk_reward import (
    build_trade_setup,
    compute_leg,
    resolve_direction,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# compute_leg — long
# ---------------------------------------------------------------------------


def test_compute_leg_long_normal():
    leg = compute_leg(
        entry=Decimal("70000"),
        stop=Decimal("65000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert leg is not None
    assert leg.risk_pct == Decimal("7.14")
    assert leg.reward_pct == Decimal("11.43")
    assert leg.rr_ratio == Decimal("1.60")


def test_compute_leg_long_price_triangle_mismatch():
    # stop above entry — invalid for long.
    leg = compute_leg(
        entry=Decimal("70000"),
        stop=Decimal("72000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert leg is None


def test_compute_leg_degenerate_entry_equals_stop():
    leg = compute_leg(
        entry=Decimal("70000"),
        stop=Decimal("70000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert leg is None


# ---------------------------------------------------------------------------
# compute_leg — short (explicit opt-in only; this is a pure-arithmetic test,
# opt-in is enforced by resolve_direction, not compute_leg).
# ---------------------------------------------------------------------------


def test_compute_leg_short_normal():
    leg = compute_leg(
        entry=Decimal("100"),
        stop=Decimal("110"),
        target=Decimal("85"),
        direction="short",
    )
    assert leg is not None
    assert leg.risk_pct == Decimal("10.00")
    assert leg.reward_pct == Decimal("15.00")
    assert leg.rr_ratio == Decimal("1.50")


def test_compute_leg_short_price_triangle_mismatch():
    # target above entry — invalid for short.
    leg = compute_leg(
        entry=Decimal("100"),
        stop=Decimal("110"),
        target=Decimal("105"),
        direction="short",
    )
    assert leg is None


def test_compute_leg_short_sell_example_from_issue():
    """SELL/short example from the ROB-690 motivating case.

    entry=2,424,000 / stop=2,600,000 / target=2,100,000. With direction
    resolved to explicit short, the triangle is valid (target < entry <
    stop) and produces risk 7.26% / reward 13.37% / R:R 1.84 — positive
    magnitudes (the loss/gain framing is carried by ``direction``, not by a
    signed number).
    """
    leg = compute_leg(
        entry=Decimal("2424000"),
        stop=Decimal("2600000"),
        target=Decimal("2100000"),
        direction="short",
    )
    assert leg is not None
    assert leg.risk_pct == Decimal("7.26")
    assert leg.reward_pct == Decimal("13.37")
    assert leg.rr_ratio == Decimal("1.84")


def test_compute_leg_rr_ratio_equals_pct_ratio_identity():
    """rr_ratio (distance ratio) equals the pct-based ratio (entry normalization
    cancels out) — confirms the two are computed consistently."""
    entry, stop, target = Decimal("70000"), Decimal("65000"), Decimal("78000")
    leg = compute_leg(entry=entry, stop=stop, target=target, direction="long")
    assert leg is not None
    pct_ratio = leg.reward_pct / leg.risk_pct
    # Both are independently 2dp-quantized so allow a small tolerance from
    # compounding rounding, but they should agree to within 1 quantum.
    assert abs(pct_ratio - leg.rr_ratio) <= Decimal("0.01")


# ---------------------------------------------------------------------------
# rounding
# ---------------------------------------------------------------------------


def test_compute_leg_quantizes_to_two_dp_round_half_up():
    # risk_distance/entry*100 = 1/8*100 = 12.5 exactly -> should stay 12.50,
    # not a rounding edge case; use a genuine half-up boundary instead.
    # entry=100, stop=99.995 -> risk_pct = 0.005/100*100 = 0.005 -> rounds to 0.01 (half up).
    leg = compute_leg(
        entry=Decimal("100"),
        stop=Decimal("99.995"),
        target=Decimal("200"),
        direction="long",
    )
    assert leg is not None
    assert leg.risk_pct == Decimal("0.01")


# ---------------------------------------------------------------------------
# resolve_direction matrix (D4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "side,intent,item_kind,explicit_direction,expected",
    [
        ("buy", "buy_review", "action", None, "long"),
        ("sell", "sell_review", "action", None, "exit"),
        ("sell", "buy_review", "action", None, "exit"),
        (None, "sell_review", "action", None, "exit"),
        (None, "buy_review", "action", "short", "short"),
        ("buy", "buy_review", "action", "long", "long"),
        (None, "buy_review", "action", None, "long"),
        (None, "trend_recovery_review", "action", None, "long"),
        (None, "rebalance_review", "action", None, "unknown"),
        (None, "risk_review", "risk", None, "unknown"),
    ],
)
def test_resolve_direction_matrix(
    side, intent, item_kind, explicit_direction, expected
):
    assert (
        resolve_direction(
            side=side,
            intent=intent,
            item_kind=item_kind,
            explicit_direction=explicit_direction,
        )
        == expected
    )


# ---------------------------------------------------------------------------
# build_trade_setup — multi-leg headline (D6)
# ---------------------------------------------------------------------------


def test_build_trade_setup_single_leg():
    setup = build_trade_setup(
        entry_levels=[Decimal("70000")],
        quantities=[None],
        stop=Decimal("65000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert setup.status == "computed"
    assert len(setup.legs) == 1
    assert setup.headline is not None
    assert setup.headline.entry == Decimal("70000")
    assert setup.headline.rr_ratio == Decimal("1.60")


def test_build_trade_setup_multi_leg_simple_average_no_quantities():
    setup = build_trade_setup(
        entry_levels=[Decimal("70000"), Decimal("68000")],
        quantities=[None, None],
        stop=Decimal("65000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert setup.status == "computed"
    assert len(setup.legs) == 2
    # simple average: (70000+68000)/2 = 69000
    assert setup.headline is not None
    assert setup.headline.entry == Decimal("69000")


def test_build_trade_setup_multi_leg_quantity_weighted_average():
    setup = build_trade_setup(
        entry_levels=[Decimal("70000"), Decimal("68000")],
        quantities=[Decimal("3"), Decimal("1")],
        stop=Decimal("65000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert setup.status == "computed"
    # weighted: (70000*3 + 68000*1) / 4 = (210000+68000)/4 = 278000/4 = 69500
    assert setup.headline is not None
    assert setup.headline.entry == Decimal("69500")


def test_build_trade_setup_partial_quantities_falls_back_to_simple_average():
    # Not every level has qty > 0 -> simple average, not weighted.
    setup = build_trade_setup(
        entry_levels=[Decimal("70000"), Decimal("68000")],
        quantities=[Decimal("3"), None],
        stop=Decimal("65000"),
        target=Decimal("78000"),
        direction="long",
    )
    assert setup.status == "computed"
    assert setup.headline is not None
    assert setup.headline.entry == Decimal("69000")


def test_build_trade_setup_mismatch_fails_closed_empty_legs():
    setup = build_trade_setup(
        entry_levels=[Decimal("70000")],
        quantities=[None],
        stop=Decimal("72000"),  # stop above entry -> invalid for long
        target=Decimal("78000"),
        direction="long",
    )
    assert setup.status == "direction_price_mismatch"
    assert setup.legs == ()
    assert setup.headline is None


def test_build_trade_setup_degenerate_fails_closed():
    setup = build_trade_setup(
        entry_levels=[Decimal("70000")],
        quantities=[None],
        stop=Decimal("70000"),  # entry == stop -> degenerate risk
        target=Decimal("78000"),
        direction="long",
    )
    assert setup.status == "degenerate_risk"
    assert setup.legs == ()
    assert setup.headline is None
