"""§176차 — ``buy.underwater_support_net`` eligibility, sizing, and approval lane.

The fixtures are the three real KIS adds the 2026-09-07 session proposed
(week1-exec-report §1-1 / cash-active-week1-plan §b-2), not invented numbers,
so a regression here is measurable against what actually happened that day.
"""

from decimal import Decimal

import pytest

from app.services.order_proposals.auto_approve import (
    AutoApproveLimits,
    evaluate_auto_approve_eligibility,
)
from app.services.trading_policy_service import load_trading_policy
from app.services.underwater_support_net import (
    REASON_IMPROVEMENT,
    REASON_LOSS_ABOVE_FLOOR,
    REASON_NOT_UNDERWATER,
    REASON_SIZING,
    REASON_SUPPORT_BAND,
    REASON_SUPPORT_FAMILIES,
    REASON_SUPPORT_STRENGTH,
    REASON_THESIS,
    UnderwaterLot,
    UnderwaterPolicyError,
    evaluate_underwater_support_net,
    load_underwater_tier_policy,
)


def _lot(**overrides) -> UnderwaterLot:
    """015760 한국전력 as measured 2026-09-07, unless a field is overridden."""

    base = {
        "symbol": "015760",
        "market": "kr",
        "quantity": Decimal("100"),
        "average_cost": Decimal("43637.5"),
        "current_price": Decimal("31950"),
        "support_price": Decimal("30182"),
        "support_strength": "moderate",
        "support_source_families": ("fib", "bb_lower"),
        "thesis_alive": True,
        "rung_price": Decimal("30150"),
    }
    base.update(overrides)
    return UnderwaterLot(**base)


# --------------------------------------------------------------------------
# The three real 2026-09-07 KIS adds pass, with the sizes that were proposed.
# --------------------------------------------------------------------------


def test_015760_passes_and_reproduces_the_measured_size_and_improvement():
    verdict = evaluate_underwater_support_net(_lot())

    assert verdict["eligible"] is True
    assert verdict["reasons"] == []
    # 50% of 100 x 31,950 = 1,597,500 -> floor(1,597,500 / 30,150) = 52 shares.
    assert verdict["add_quantity"] == "52"
    assert verdict["add_notional"] == "1567800"
    assert verdict["new_average_cost"] == "39023.3553"
    # +37.95% -> +23.36%, i.e. 14.59 percentage points, far past the 3pp floor.
    assert verdict["required_rebound_pct_before"] == "37.9464"
    assert verdict["required_rebound_pct_after"] == "23.3602"
    assert Decimal(verdict["required_rebound_improvement_pct_points"]) > Decimal("3")


def test_196170_and_035420_pass_with_their_measured_sizes():
    alteogen = evaluate_underwater_support_net(
        _lot(
            symbol="196170",
            quantity=Decimal("6"),
            average_cost=Decimal("313000"),
            current_price=Decimal("284000"),
            support_price=Decimal("274763"),
            rung_price=Decimal("274500"),
        )
    )
    naver = evaluate_underwater_support_net(
        _lot(
            symbol="035420",
            quantity=Decimal("3"),
            average_cost=Decimal("241500"),
            current_price=Decimal("214000"),
            support_price=Decimal("202994"),
            rung_price=Decimal("202500"),
        )
    )

    assert alteogen["eligible"] is True
    assert alteogen["add_quantity"] == "3"
    assert alteogen["add_notional"] == "823500"
    assert naver["eligible"] is True
    assert naver["add_quantity"] == "1"
    assert naver["add_notional"] == "202500"


def test_size_cap_is_50_percent_of_the_existing_position_notional():
    verdict = evaluate_underwater_support_net(_lot())

    position = Decimal(verdict["position_notional"])
    assert position == Decimal("100") * Decimal("31950")
    assert Decimal(verdict["max_add_notional"]) == position / 2
    # The cap binds the ADD, so the add never exceeds it even after tick floor.
    assert Decimal(verdict["add_notional"]) <= Decimal(verdict["max_add_notional"])


# --------------------------------------------------------------------------
# Rejections — one clause at a time, from the same base lot.
# --------------------------------------------------------------------------


def test_profitable_lot_is_rejected():
    """§139차's crypto twin admits only winners; this tier admits only losers."""

    verdict = evaluate_underwater_support_net(
        _lot(average_cost=Decimal("25000"), current_price=Decimal("31950"))
    )

    assert verdict["eligible"] is False
    assert REASON_NOT_UNDERWATER in verdict["reasons"]


def test_shallow_loss_above_the_floor_is_rejected():
    # -5% is a loss but not one this tier acts on.
    verdict = evaluate_underwater_support_net(
        _lot(average_cost=Decimal("33631.58"), current_price=Decimal("31950"))
    )

    assert verdict["eligible"] is False
    assert REASON_LOSS_ABOVE_FLOOR in verdict["reasons"]


def test_loss_floor_is_inclusive_at_exactly_minus_eight_percent():
    """ "손실 <= -8%" is inclusive; a lot exactly on the line qualifies."""

    lot = _lot(average_cost=Decimal("100"), current_price=Decimal("92"))
    verdict = evaluate_underwater_support_net(lot)

    assert verdict["unrealized_pnl_pct"] == "-8.0000"
    assert REASON_LOSS_ABOVE_FLOOR not in verdict["reasons"]
    assert REASON_NOT_UNDERWATER not in verdict["reasons"]


def test_weak_support_is_rejected():
    verdict = evaluate_underwater_support_net(_lot(support_strength="weak"))

    assert verdict["eligible"] is False
    assert REASON_SUPPORT_STRENGTH in verdict["reasons"]


def test_single_source_family_is_rejected():
    verdict = evaluate_underwater_support_net(
        _lot(support_source_families=("bb_lower",))
    )

    assert verdict["eligible"] is False
    assert REASON_SUPPORT_FAMILIES in verdict["reasons"]


def test_duplicate_source_names_do_not_count_twice():
    """Two readings of one family are one family."""

    verdict = evaluate_underwater_support_net(
        _lot(support_source_families=("bb_lower", "bb_lower"))
    )

    assert REASON_SUPPORT_FAMILIES in verdict["reasons"]


@pytest.mark.parametrize(
    ("support_price", "why"),
    [
        (Decimal("31580"), "too close: -1.16%, inside the -3% edge"),
        (Decimal("27000"), "too far: -15.5%, past the -12% edge"),
    ],
)
def test_support_outside_the_band_is_rejected(support_price, why):
    verdict = evaluate_underwater_support_net(_lot(support_price=support_price))

    assert verdict["eligible"] is False, why
    assert REASON_SUPPORT_BAND in verdict["reasons"]


def test_dead_thesis_is_rejected_and_belongs_to_the_loss_cut_branch():
    verdict = evaluate_underwater_support_net(_lot(thesis_alive=False))

    assert verdict["eligible"] is False
    assert REASON_THESIS in verdict["reasons"]


def test_improvement_below_three_points_is_rejected():
    """The clause that rejects an add which spends cash and moves nothing.

    Shape: a small lot at exactly the -8% floor whose 50% cap admits exactly
    one whole share. The add is legal on every other clause -- moderate
    two-family support at -3%, live thesis, inside the cap -- and it still
    fails, because one share against three only pulls the escape point from
    +9.78% to +6.83%, which is 2.95 percentage points.

    This is the near miss on purpose: at 2.95pp the clause is doing real work
    rather than rejecting something already obviously bad.
    """

    verdict = evaluate_underwater_support_net(
        _lot(
            quantity=Decimal("3"),
            average_cost=Decimal("125000"),
            current_price=Decimal("115000"),
            support_price=Decimal("111550"),
            rung_price=Decimal("111550"),
        )
    )

    assert verdict["eligible"] is False
    assert verdict["reasons"] == [REASON_IMPROVEMENT]
    assert verdict["add_quantity"] == "1"
    assert verdict["required_rebound_pct_before"] == "9.7826"
    assert verdict["required_rebound_pct_after"] == "6.8295"
    assert verdict["required_rebound_improvement_pct_points"] == "2.9532"


def test_size_cap_admitting_no_whole_share_is_rejected_as_sizing():
    """267260 HD현대일렉 as measured: 1 share 690,000 > the 367,500 cap.

    Reported as a sizing failure rather than an improvement failure, because
    the reason the add does nothing is that there is no add.
    """

    verdict = evaluate_underwater_support_net(
        _lot(
            symbol="267260",
            quantity=Decimal("1"),
            average_cost=Decimal("892000"),
            current_price=Decimal("735000"),
            support_price=Decimal("690928"),
            rung_price=Decimal("690000"),
        )
    )

    assert verdict["eligible"] is False
    assert verdict["reasons"] == [REASON_SIZING]
    assert verdict["add_quantity"] == "0"


def test_every_failing_clause_is_reported_not_just_the_first():
    verdict = evaluate_underwater_support_net(
        _lot(support_strength="weak", thesis_alive=False)
    )

    assert REASON_SUPPORT_STRENGTH in verdict["reasons"]
    assert REASON_THESIS in verdict["reasons"]


# --------------------------------------------------------------------------
# Scope statements the policy makes, asserted rather than left to prose.
# --------------------------------------------------------------------------


def test_per_symbol_notional_band_does_not_apply_to_a_held_lot_add():
    """The KR band is 200,000-400,000 *for new entries*; the add is 1,567,800.

    The band is not consulted by this evaluator at all, and the tier records
    that as a scope statement rather than a waiver. Both halves are asserted:
    the add clears the tier while sitting far outside the band, and the band's
    own semantics still say "new entries".
    """

    document = load_trading_policy()
    band = document.thresholds["buy.per_symbol_notional_krw_range"]
    verdict = evaluate_underwater_support_net(_lot())

    assert band.value == [200000, 400000]
    assert "new entries" in band.semantics
    assert Decimal(verdict["add_notional"]) > Decimal("400000")
    assert verdict["eligible"] is True

    tier = [
        tier
        for tier in document.decision_rules["buy.underwater_support_net"].tiers
        if tier.id == "underwater_support_net"
    ][0]
    assert tier.conditions["per_symbol_notional_band_applies"] is False
    assert (
        tier.conditions["per_symbol_notional_band_scope_reason"]
        == "bands_are_scoped_to_new_entries"
    )


def test_required_rebound_is_derived_from_the_existing_loss_guard_key():
    """The escape price is the loss guard's, not a number restated here."""

    document = load_trading_policy()
    contract = load_underwater_tier_policy(document)

    assert contract.loss_guard_multiple == Decimal(
        str(document.thresholds["sell.loss_guard_min_multiple"].value)
    )


def test_missing_tier_fails_closed_rather_than_defaulting(monkeypatch):
    document = load_trading_policy()
    stripped = document.model_copy(
        update={
            "decision_rules": {
                key: rule
                for key, rule in document.decision_rules.items()
                if key != "buy.underwater_support_net"
            }
        }
    )

    with pytest.raises(UnderwaterPolicyError):
        load_underwater_tier_policy(stripped)


# --------------------------------------------------------------------------
# The approval lane: no new surface, and no cap raised.
# --------------------------------------------------------------------------


class _Group:
    action = "place"
    order_type = "limit"
    exit_intent = None
    account_mode = "kis_live"
    market = "equity_kr"
    side = "buy"
    symbol = "015760"
    thesis = "underwater support net add"
    strategy = "underwater_support_net"
    rationale = None
    lot_context = None
    proposer = "session"
    target_broker_order_id = None


class _Rung:
    side = "buy"
    quantity = Decimal("52")
    limit_price = Decimal("30150")


def _limits(**overrides) -> AutoApproveLimits:
    base = {
        "mode": "off",
        "min_distance_pct": Decimal("3"),
        "per_order_cap": Decimal("2000000"),
        "daily_cap": Decimal("5000000"),
        "policy_version": "test",
        "breakeven_band_pct": Decimal("1"),
        "round_trip_cost_bps": Decimal("47.4"),
    }
    base.update(overrides)
    return AutoApproveLimits(**base)


def test_underwater_rung_is_auto_approvable_under_the_existing_off_mode_rules():
    """No tier name is an input; the -3% band edge IS the 3% distance rule."""

    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_Rung(),
        preview={"success": True, "current_price": "31950"},
        limits=_limits(),
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is True, decision.reason


def test_an_over_cap_underwater_rung_still_falls_back_to_a_human_card():
    """§176차 raises no cap: the per-order boundary keeps its shape."""

    class _BigRung:
        side = "buy"
        quantity = Decimal("100")
        limit_price = Decimal("30150")

    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_BigRung(),
        preview={"success": True, "current_price": "31950"},
        limits=_limits(),
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"
