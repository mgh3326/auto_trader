"""§177차 — ``buy.underwater_support_net`` eligibility, sizing, and approval lane.

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
from app.services.order_proposals.buying_power import (
    BuyingPowerCache,
    BuyingPowerKey,
    required_cash,
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
    # The floor admitted whole shares on its own; the #877 exception did not
    # fire, so the D+20 cohort tag stays off.
    assert verdict["rounded_up_to_one_share"] is False


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
    assert alteogen["rounded_up_to_one_share"] is False
    assert naver["eligible"] is True
    assert naver["add_quantity"] == "1"
    assert naver["add_notional"] == "202500"
    # floor(321,000 / 202,500) = 1 — an ordinary floor result, not the #877
    # exception, so the tag stays off.
    assert naver["rounded_up_to_one_share"] is False


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
    # floor(172,500 / 111,550) is 1 on its own — the #877 exception did not
    # fire here, so the D+20 tag stays off.
    assert verdict["rounded_up_to_one_share"] is False
    assert verdict["required_rebound_pct_before"] == "9.7826"
    assert verdict["required_rebound_pct_after"] == "6.8295"
    assert verdict["required_rebound_improvement_pct_points"] == "2.9532"


def test_one_share_exception_sizes_a_below_one_share_computation_to_exactly_one():
    """#877 — 267260 HD현대일렉, the measured lot that motivated the exception.

    50% of the position (367,500) floors to zero shares at the 690,000 rung.
    Before #877 this was a REASON_SIZING rejection; the approved exception
    now sizes the add to exactly one share and tags it for the
    underwater-d20-v1 cohort. Every other clause is evaluated on that share,
    unchanged: -17.6% underwater, moderate two-family support inside the
    band, live thesis, and the improvement the single share buys (13.88pp)
    is far past the 3pp floor.
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

    assert verdict["add_quantity"] == "1"
    assert verdict["rounded_up_to_one_share"] is True
    # The add is allowed to exceed the 50% notional cap — that is the
    # exception — and the tag is what records that it did.
    assert Decimal(verdict["add_notional"]) == Decimal("690000")
    assert Decimal(verdict["add_notional"]) > Decimal(verdict["max_add_notional"])
    assert verdict["required_rebound_pct_before"] == "22.5741"
    assert verdict["required_rebound_pct_after"] == "8.6952"
    assert verdict["required_rebound_improvement_pct_points"] == "13.8789"
    assert verdict["eligible"] is True
    assert verdict["reasons"] == []


def test_one_share_exception_does_not_rescue_a_lot_failing_other_clauses():
    """The exception changes SIZING only. A rounded one-share add on a lot
    whose thesis is dead is still ineligible — the tag records that the
    exception fired, it does not override the verdict."""

    verdict = evaluate_underwater_support_net(
        _lot(
            symbol="267260",
            quantity=Decimal("1"),
            average_cost=Decimal("892000"),
            current_price=Decimal("735000"),
            support_price=Decimal("690928"),
            rung_price=Decimal("690000"),
            thesis_alive=False,
        )
    )

    assert verdict["add_quantity"] == "1"
    assert verdict["rounded_up_to_one_share"] is True
    assert verdict["eligible"] is False
    assert verdict["reasons"] == [REASON_THESIS]


def test_unpriced_rung_still_fails_as_sizing_despite_the_exception():
    """Rung price <= 0 means there is nothing to size one share against;
    the exception cannot conjure a price, so the sizing rejection stands."""

    verdict = evaluate_underwater_support_net(_lot(rung_price=Decimal("0")))

    assert verdict["eligible"] is False
    assert verdict["reasons"] == [REASON_SIZING]
    assert verdict["add_quantity"] == "0"
    assert verdict["rounded_up_to_one_share"] is False


def test_one_share_exception_off_in_policy_keeps_the_old_sizing_rejection():
    """The exception is a declared flag, not a silent default: a policy that
    carries it as false must reproduce the pre-#877 sizing rejection."""

    document = load_trading_policy()
    rule = document.decision_rules["buy.underwater_support_net"]
    tier = rule.tiers[0]
    disabled_conditions = dict(tier.conditions)
    disabled_conditions["one_share_exception_for_adds"] = False
    disabled = rule.model_copy(
        update={"tiers": [tier.model_copy(update={"conditions": disabled_conditions})]}
    )
    drifted = document.model_copy(
        update={
            "decision_rules": {
                **document.decision_rules,
                "buy.underwater_support_net": disabled,
            }
        }
    )

    verdict = evaluate_underwater_support_net(
        _lot(
            symbol="267260",
            quantity=Decimal("1"),
            average_cost=Decimal("892000"),
            current_price=Decimal("735000"),
            support_price=Decimal("690928"),
            rung_price=Decimal("690000"),
        ),
        policy=drifted,
    )

    assert verdict["eligible"] is False
    assert verdict["reasons"] == [REASON_SIZING]
    assert verdict["add_quantity"] == "0"
    assert verdict["rounded_up_to_one_share"] is False


def test_missing_one_share_exception_key_fails_closed():
    """Fail-closed, same as every other tier literal: a hand-built policy
    missing the key must raise rather than silently choose a behavior."""

    document = load_trading_policy()
    rule = document.decision_rules["buy.underwater_support_net"]
    tier = rule.tiers[0]
    trimmed_conditions = {
        key: value
        for key, value in tier.conditions.items()
        if key != "one_share_exception_for_adds"
    }
    trimmed = rule.model_copy(
        update={"tiers": [tier.model_copy(update={"conditions": trimmed_conditions})]}
    )
    drifted = document.model_copy(
        update={
            "decision_rules": {
                **document.decision_rules,
                "buy.underwater_support_net": trimmed,
            }
        }
    )

    with pytest.raises(UnderwaterPolicyError):
        load_underwater_tier_policy(drifted)


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
    """§177차 raises no cap: the per-order boundary keeps its shape."""

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


# --------------------------------------------------------------------------
# #877 — the one-share exception's hard upper bounds, at exact boundaries.
# A rounded-up one-share rung is an ordinary rung to the approval surface:
# the per-order cap, the daily cap, and orderable cash all still bind, at
# exactly their existing operators (> cap rejects, == cap passes).
# --------------------------------------------------------------------------


class _OneShareKrRung:
    """The rounded 1-share rung, priced so notional lands on the KR cap."""

    def __init__(self, limit_price: Decimal):
        self.side = "buy"
        self.quantity = Decimal("1")
        self.limit_price = limit_price


def test_one_share_rung_at_exactly_the_kr_per_order_cap_is_approvable():
    """2,000,000 KRW is the cap itself, not over it — the add passes."""

    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_OneShareKrRung(Decimal("2000000")),
        preview={"success": True, "current_price": "2100000"},
        limits=_limits(),
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is True, decision.reason


def test_one_share_rung_one_won_over_the_kr_per_order_cap_is_not_automatic():
    """The exception cannot push an automatic order past 2,000,000 KRW."""

    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_OneShareKrRung(Decimal("2000001")),
        preview={"success": True, "current_price": "2100000"},
        limits=_limits(),
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


class _OneShareUsGroup(_Group):
    market = "equity_us"
    symbol = "AVUV"


class _OneShareUsRung:
    def __init__(self, limit_price: Decimal):
        self.side = "buy"
        self.quantity = Decimal("1")
        self.limit_price = limit_price


def test_one_share_rung_at_exactly_the_us_per_order_cap_is_approvable():
    decision = evaluate_auto_approve_eligibility(
        group=_OneShareUsGroup(),
        rung=_OneShareUsRung(Decimal("1500")),
        preview={"success": True, "current_price": "1600"},
        limits=_limits(per_order_cap=Decimal("1500"), daily_cap=Decimal("20000")),
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is True, decision.reason


def test_one_share_rung_over_the_us_per_order_cap_is_not_automatic():
    decision = evaluate_auto_approve_eligibility(
        group=_OneShareUsGroup(),
        rung=_OneShareUsRung(Decimal("1500.01")),
        preview={"success": True, "current_price": "1600"},
        limits=_limits(per_order_cap=Decimal("1500"), daily_cap=Decimal("20000")),
        daily_notional=Decimal("0"),
    )

    assert decision.eligible is False
    assert decision.reason == "per_order_cap_exceeded"


def test_one_share_rung_landing_exactly_on_the_daily_cap_is_approvable():
    """daily_notional + notional == daily_cap is still inside the bound."""

    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_OneShareKrRung(Decimal("30150")),
        preview={"success": True, "current_price": "31950"},
        limits=_limits(),
        daily_notional=Decimal("5000000") - Decimal("30150"),
    )

    assert decision.eligible is True, decision.reason


def test_one_share_rung_one_won_over_the_daily_cap_is_not_automatic():
    decision = evaluate_auto_approve_eligibility(
        group=_Group(),
        rung=_OneShareKrRung(Decimal("30150")),
        preview={"success": True, "current_price": "31950"},
        limits=_limits(),
        daily_notional=Decimal("5000000") - Decimal("30150") + Decimal("1"),
    )

    assert decision.eligible is False
    assert decision.reason == "daily_cap_exceeded"


@pytest.mark.asyncio
async def test_one_share_add_is_bounded_by_orderable_cash_at_exact_boundary():
    """The create-time buying-power claim is `available >= required`:
    exactly the rounded notional claims successfully, one won short does
    not — a cash-short one-share add cannot produce an order intent."""

    cache = BuyingPowerCache(ttl_seconds=60.0)
    key = BuyingPowerKey("toss_live", "acct-1", "KRW")
    rung_price = Decimal("690000")  # the rounded HD현대일렉 share above
    required = required_cash(quantity=Decimal("1"), limit_price=rung_price, preview={})
    assert required == rung_price

    claim = await cache.claim(key, required, loader=lambda: _async_const(required))
    assert claim.available == required
    assert claim.token is not None

    short_cache = BuyingPowerCache(ttl_seconds=60.0)
    short = required - Decimal("1")
    claim = await short_cache.claim(key, required, loader=lambda: _async_const(short))
    assert claim.available == short
    assert claim.token is None


async def _async_const(value: Decimal) -> Decimal:
    return value
