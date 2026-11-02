"""Pure trigger and plan rules (ROB-1460). No database, no broker."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from app.services.brokers.upbit.orders import adjust_price_to_upbit_unit
from app.services.mechanical_loss_cut.rules import (
    AddLevel,
    CutDecision,
    ExitPlanInputError,
    LotKey,
    Skip,
    add_plan,
    backfill_plan,
    bounded_limit_price,
    default_stop,
    entry_plan,
    evaluate_add_against_plan,
    evaluate_lot,
    is_parking_instrument,
    parse_exit_plan_input,
)
from app.services.quote_parity_shadow import krx_tick_size
from tests.services.mechanical_loss_cut.support import (
    KEY,
    SESSION,
    bars_from_closes,
    held,
    params,
    plan,
)

pytestmark = pytest.mark.unit

_FALLING = [95000, 93000, 91000, 89000, 87000, 86000, 84000]


def _evaluate(**overrides):
    values = {
        "lot": held(),
        "plan": plan(),
        "bars": bars_from_closes(_FALLING),
        "params": params(),
        "price_fresh": True,
        "open_sell_quantity": Decimal("0"),
        "protected_quantity": Decimal("0"),
        "last_decision_session": None,
    }
    values.update(overrides)
    return evaluate_lot(**values)


def _with_trigger(**changes):
    base = params()
    return base.model_copy(update={"trigger": base.trigger.model_copy(update=changes)})


def test_stop_hit_on_the_plan_is_a_cut():
    decision = _evaluate()

    assert isinstance(decision, CutDecision)
    assert decision.stop_price == Decimal("85000")
    assert decision.close == Decimal("84000")
    assert decision.intended_quantity == Decimal("10")
    assert decision.backfilled is False


def test_support_break_is_a_cut():
    """Addendum 1(b): the close falls through a support-based stop."""
    closes = [90000, 89000, 88500, 88000, 87500, 87000, 83000]

    decision = _evaluate(bars=bars_from_closes(closes), plan=plan(stop="86000"))

    assert isinstance(decision, CutDecision)
    assert decision.episode_start == SESSION


def test_underwater_above_the_planned_stop_is_not_cut():
    """Addendum 2: -10% from cost, but the recorded stop has not been reached."""
    closes = [95000, 94000, 93000, 92000, 91000, 90500, 90000]

    result = _evaluate(bars=bars_from_closes(closes))

    assert result == Skip("above_planned_stop", result.details)
    assert result.details["unrealized_pct"] == "-10"


def test_underwater_but_recovering_is_not_cut_even_below_the_stop():
    """Addendum 1(a): below cost and below stop, but the close is rising."""
    closes = [70000, 71000, 72000, 74000, 76000, 78000, 80000]

    result = _evaluate(bars=bars_from_closes(closes))

    assert isinstance(result, Skip)
    assert result.reason == "rising"


def test_rising_check_that_cannot_be_made_does_not_pass():
    result = _evaluate(bars=bars_from_closes([84000, 83000]))

    assert isinstance(result, Skip)
    assert result.reason == "price_missing"


def test_a_lot_above_cost_is_never_a_loss_cut():
    result = _evaluate(lot=held(average_cost="80000"), plan=plan(stop="90000"))

    assert isinstance(result, Skip)
    assert result.reason == "not_underwater"


def test_floor_must_also_be_reached():
    closes = [101000, 100500, 100000, 99500, 99000, 98500, 98000]

    result = _evaluate(bars=bars_from_closes(closes), plan=plan(stop="99000"))

    assert isinstance(result, Skip)
    assert result.reason == "floor_not_reached"


@pytest.mark.parametrize("bars", [[], None])
def test_missing_price_never_triggers(bars):
    result = _evaluate(bars=bars or [])

    assert isinstance(result, Skip)
    assert result.reason == "price_missing"


def test_stale_price_never_triggers():
    result = _evaluate(price_fresh=False)

    assert isinstance(result, Skip)
    assert result.reason == "price_stale"


def test_frozen_series_never_triggers():
    frozen = [84000] * 7
    bars = [
        replace(bar, high=bar.close, low=bar.close, open=bar.close)
        for bar in bars_from_closes(frozen)
    ]

    result = _evaluate(bars=bars)

    assert isinstance(result, Skip)
    assert result.reason == "halted_suspect"


@pytest.mark.parametrize(
    ("symbol", "market"),
    [
        ("SGOV", "equity_us"),
        ("BIL", "equity_us"),
        ("459580", "equity_kr"),
        ("357870", "equity_kr"),
    ],
)
def test_parking_instruments_are_never_cut(symbol, market):
    key = LotKey("kis_live", market, symbol)

    result = _evaluate(lot=held(key=key), plan=plan(key=key))

    assert isinstance(result, Skip)
    assert result.reason == "parking_instrument"


def test_parking_exclusion_covers_an_account_outside_the_authorization_tuple():
    assert is_parking_instrument("SGOV", "equity_us") is True
    assert is_parking_instrument(" sgov ", "equity_us") is True
    assert is_parking_instrument("SGOV", "equity_kr") is False
    assert is_parking_instrument("SGOVX", "equity_us") is False
    assert is_parking_instrument(None, "equity_us") is False


def test_lot_covered_by_an_open_sell_is_not_double_sold():
    result = _evaluate(open_sell_quantity=Decimal("10"))

    assert isinstance(result, Skip)
    assert result.reason == "covered_by_open_sell"


def test_partly_covered_lot_sells_only_the_rest():
    decision = _evaluate(open_sell_quantity=Decimal("4"))

    assert isinstance(decision, CutDecision)
    assert decision.intended_quantity == Decimal("6")


def test_protected_quantity_is_not_sold():
    decision = _evaluate(protected_quantity=Decimal("3"))

    assert isinstance(decision, CutDecision)
    assert decision.intended_quantity == Decimal("7")


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("open_sell_quantity", "open_sell_unknown"),
        ("protected_quantity", "protected_unknown"),
    ],
)
def test_unknown_quantities_never_trigger(field, reason):
    result = _evaluate(**{field: None})

    assert isinstance(result, Skip)
    assert result.reason == reason


def test_max_lot_fraction_caps_and_floors_to_whole_shares():
    base = params()
    capped = base.model_copy(
        update={"order": base.order.model_copy(update={"max_lot_fraction": 0.25})}
    )

    decision = _evaluate(params=capped)

    assert isinstance(decision, CutDecision)
    assert decision.intended_quantity == Decimal("2")


def test_fraction_below_one_share_does_not_round_up():
    base = params()
    capped = base.model_copy(
        update={"order": base.order.model_copy(update={"max_lot_fraction": 0.05})}
    )

    result = _evaluate(params=capped)

    assert isinstance(result, Skip)
    assert result.reason == "below_one_unit"


def test_cooldown_counts_sessions_since_the_last_decision():
    two_sessions_ago = SESSION - timedelta(days=2)

    result = _evaluate(last_decision_session=two_sessions_ago)

    assert isinstance(result, Skip)
    assert result.reason == "cooldown"
    assert result.details["sessions_since_last"] == "2"


def test_one_trigger_key_across_runs_of_the_same_episode():
    first = _evaluate()
    closes = [*_FALLING[1:], 83000]
    later = _evaluate(bars=bars_from_closes(closes, end=SESSION + timedelta(days=1)))

    assert isinstance(first, CutDecision)
    assert isinstance(later, CutDecision)
    assert first.trigger_key == later.trigger_key


def test_a_run_longer_than_the_window_keeps_one_trigger_key():
    """The window rolls forward each day. The key must not roll with it."""
    below = [84000, 83000, 82000, 81000, 80000, 79000, 78000]

    today = _evaluate(bars=bars_from_closes(below))
    tomorrow = _evaluate(
        bars=bars_from_closes([*below[1:], 77000], end=SESSION + timedelta(days=1))
    )

    assert isinstance(today, CutDecision)
    assert isinstance(tomorrow, CutDecision)
    assert today.episode_start is None
    assert today.trigger_key == tomorrow.trigger_key


def test_a_recovery_then_a_new_break_is_a_new_trigger():
    first_break = [90000, 89000, 88000, 87000, 86000, 86000, 84000]
    second_break = [84000, 84000, 90000, 89000, 88000, 87000, 84000]

    first = _evaluate(bars=bars_from_closes(first_break))
    second = _evaluate(
        bars=bars_from_closes(second_break, end=SESSION + timedelta(days=6))
    )

    assert isinstance(first, CutDecision)
    assert isinstance(second, CutDecision)
    assert first.trigger_key != second.trigger_key


def test_a_new_plan_version_is_a_new_trigger():
    first = _evaluate()
    second = _evaluate(plan=plan(version=2))

    assert isinstance(first, CutDecision)
    assert isinstance(second, CutDecision)
    assert first.trigger_key != second.trigger_key


def test_n_day_low_family_needs_a_fresh_low():
    tuned = _with_trigger(family="plan_stop_and_n_day_low", n_day_low_sessions=5)
    closes = [86000, 86000, 85000, 82000, 85000, 84500, 84400]

    assert isinstance(_evaluate(params=tuned, bars=bars_from_closes(closes)), Skip)
    assert (
        _evaluate(params=tuned, bars=bars_from_closes(closes)).reason == "not_n_day_low"
    )
    assert isinstance(_evaluate(params=tuned), CutDecision)


def test_trailing_family_needs_the_drop():
    tuned = _with_trigger(
        family="plan_stop_and_trailing_drop",
        trailing_drop_pct=20,
        trailing_lookback_sessions=7,
    )

    shallow = _evaluate(params=tuned)
    assert isinstance(shallow, Skip)
    assert shallow.reason == "trailing_drop_not_reached"

    closes = [110000, 108000, 106000, 100000, 95000, 90000, 84000]
    assert isinstance(
        _evaluate(params=tuned, bars=bars_from_closes(closes)), CutDecision
    )


def test_limit_stays_inside_the_slippage_band_and_on_a_tick():
    kr = bounded_limit_price(
        "equity_kr", price=Decimal("84000"), slippage_pct=Decimal("1")
    )
    us = bounded_limit_price(
        "equity_us", price=Decimal("123.456"), slippage_pct=Decimal("1")
    )

    assert kr == Decimal("83200")
    assert kr % krx_tick_size(kr) == 0
    assert kr >= Decimal("84000") * Decimal("0.99")
    assert us == Decimal("122.23")


@pytest.mark.parametrize(
    "price",
    ["2500000", "1500000", "700000", "150000", "15000", "1500", "150", "15", "1.5"],
)
def test_upbit_limit_is_a_price_the_exchange_accepts(price):
    limit = bounded_limit_price(
        "crypto", price=Decimal(price), slippage_pct=Decimal("1")
    )

    assert Decimal(str(adjust_price_to_upbit_unit(float(limit)))) == limit
    assert limit >= Decimal(price) * Decimal("0.99")


@pytest.mark.parametrize(
    "price", ["600000", "300000", "60000", "30000", "6000", "3000", "1500"]
)
def test_krx_units_match_the_shared_ladder(price):
    limit = bounded_limit_price(
        "equity_kr", price=Decimal(price), slippage_pct=Decimal("1")
    )

    assert limit % krx_tick_size(limit) == 0


def test_buy_writes_a_plan_with_the_stated_stop_and_ladder():
    stated = parse_exit_plan_input(
        {
            "stop_price": "85000",
            "stop_basis": "support",
            "stop_reason": "60-session low",
            "add_ladder": [
                {"price": "95000", "quantity": "2"},
                {"price": "90000", "quantity": "3"},
            ],
            "max_adds": 2,
        }
    )

    draft = entry_plan(
        key=KEY,
        params=params(),
        price=Decimal("100000"),
        quantity=Decimal("10"),
        bars=[],
        stated=stated,
    )

    assert draft.event == "entry"
    assert draft.stop_price == Decimal("85000")
    assert draft.stop_basis == "support"
    assert draft.add_ladder[0] == AddLevel(Decimal("95000"), Decimal("2"))
    assert draft.max_adds == 2
    assert draft.backfilled is False


def test_buy_without_a_stated_stop_uses_the_policy_default():
    draft = entry_plan(
        key=KEY,
        params=params(),
        price=Decimal("100000"),
        quantity=Decimal("10"),
        bars=[],
        stated=None,
    )

    assert draft.stop_price == Decimal("85000")
    assert draft.stop_basis == "pct_floor"
    assert draft.add_ladder == ()


def test_add_updates_average_and_adds_used_and_keeps_the_stop_by_default():
    previous = plan(
        ladder=(AddLevel(Decimal("95000"), Decimal("5")),),
        max_adds=1,
    )

    draft = add_plan(
        previous=previous,
        params=params(),
        held_quantity=Decimal("10"),
        held_average_cost=Decimal("100000"),
        price=Decimal("90000"),
        quantity=Decimal("5"),
        stated=None,
    )

    assert draft.event == "add"
    assert draft.add_verdict == "on_ladder"
    assert draft.adds_used == 1
    assert draft.quantity == Decimal("15")
    assert draft.average_cost.quantize(Decimal("0.01")) == Decimal("96666.67")
    assert draft.stop_price == Decimal("85000")


def test_add_can_recompute_the_stop_from_the_new_average_when_policy_says_so():
    tuned = params().model_copy(update={"stop_after_add": "recompute_from_average"})

    draft = add_plan(
        previous=plan(),
        params=tuned,
        held_quantity=Decimal("10"),
        held_average_cost=Decimal("100000"),
        price=Decimal("90000"),
        quantity=Decimal("10"),
        stated=None,
    )

    assert draft.average_cost == Decimal("95000")
    assert draft.stop_price == Decimal("80750")
    assert draft.stop_basis == "pct_floor"


def test_add_with_a_stated_stop_replaces_the_plan():
    stated = parse_exit_plan_input({"stop_price": "80000", "stop_basis": "volatility"})

    draft = add_plan(
        previous=plan(),
        params=params(),
        held_quantity=Decimal("10"),
        held_average_cost=Decimal("100000"),
        price=Decimal("90000"),
        quantity=Decimal("10"),
        stated=stated,
    )

    assert draft.stop_price == Decimal("80000")
    assert draft.stop_basis == "volatility"


@pytest.mark.parametrize(
    ("price", "quantity", "kwargs", "expected"),
    [
        ("94000", "2", {}, "on_ladder"),
        ("96000", "2", {}, "above_next_level"),
        ("94000", "9", {}, "size_exceeds_level"),
        ("85000", "2", {}, "at_or_below_stop"),
        ("80000", "2", {}, "at_or_below_stop"),
        ("94000", "2", {"adds_used": 1}, "max_adds_reached"),
    ],
)
def test_add_is_evaluated_against_the_plan_before_the_stop(
    price, quantity, kwargs, expected
):
    previous = plan(
        ladder=(AddLevel(Decimal("95000"), Decimal("5")),), max_adds=1, **kwargs
    )

    verdict = evaluate_add_against_plan(
        previous, price=Decimal(price), quantity=Decimal(quantity)
    )

    assert verdict == expected


def test_add_without_a_ladder_is_recorded_as_such():
    assert (
        evaluate_add_against_plan(plan(), price=Decimal("90000"), quantity=Decimal("1"))
        == "no_ladder"
    )


def test_second_rung_of_one_proposal_does_not_spend_an_add():
    previous = plan(proposal_id="p-1")

    draft = add_plan(
        previous=previous,
        params=params(),
        held_quantity=None,
        held_average_cost=None,
        price=Decimal("95000"),
        quantity=Decimal("10"),
        stated=None,
        proposal_id="p-1",
    )

    assert draft.add_verdict == "same_proposal_tranche"
    assert draft.adds_used == 0


def test_backfill_is_marked_and_measured_from_today_cost():
    draft = backfill_plan(lot=held(), params=params(), bars=[])

    assert draft.backfilled is True
    assert draft.event == "backfill"
    assert draft.stop_price == Decimal("85000")


def test_support_stop_uses_the_low_and_cannot_be_wider_than_the_floor():
    base = params().default_stop.model_copy(update={"basis": "support"})

    near_low = bars_from_closes([96000] * 7)
    stop, basis, _ = default_stop(
        base, reference_price=Decimal("100000"), bars=near_low
    )
    assert basis == "support"
    assert stop < Decimal("96000")

    far_low = bars_from_closes([50000] * 7)
    stop, basis, _ = default_stop(base, reference_price=Decimal("100000"), bars=far_low)
    assert basis == "pct_floor"
    assert stop == Decimal("85000")


def test_volatility_stop_falls_back_to_the_floor_without_enough_bars():
    base = params().default_stop.model_copy(update={"basis": "volatility"})

    stop, basis, _ = default_stop(
        base, reference_price=Decimal("100000"), bars=bars_from_closes([96000] * 3)
    )
    assert (stop, basis) == (Decimal("85000"), "pct_floor")

    stop, basis, _ = default_stop(
        base, reference_price=Decimal("100000"), bars=bars_from_closes([96000] * 20)
    )
    assert basis == "volatility"
    assert Decimal("85000") < stop < Decimal("100000")


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("nope", "exit_plan_not_a_mapping"),
        ({"stop_price": "0", "stop_basis": "support"}, "exit_plan_stop_invalid"),
        ({"stop_price": 1.5, "stop_basis": "support"}, "exit_plan_stop_invalid"),
        ({"stop_price": "NaN", "stop_basis": "support"}, "exit_plan_stop_invalid"),
        ({"stop_price": "10", "stop_basis": "vibes"}, "exit_plan_stop_basis_invalid"),
        (
            {"stop_price": "10", "stop_basis": "support", "bogus": 1},
            "exit_plan_unknown_field",
        ),
        (
            {"stop_price": "10", "stop_basis": "support", "stop_reason": "  "},
            "exit_plan_stop_reason_invalid",
        ),
        (
            {
                "stop_price": "10",
                "stop_basis": "support",
                "add_ladder": [{"price": "9", "quantity": "1"}],
            },
            "exit_plan_ladder_not_above_stop",
        ),
        (
            {
                "stop_price": "10",
                "stop_basis": "support",
                "add_ladder": [
                    {"price": "11", "quantity": "1"},
                    {"price": "12", "quantity": "1"},
                ],
            },
            "exit_plan_ladder_not_descending",
        ),
        (
            {
                "stop_price": "10",
                "stop_basis": "support",
                "add_ladder": [{"price": "11", "quantity": "1"}],
                "max_adds": 2,
            },
            "exit_plan_max_adds_invalid",
        ),
    ],
)
def test_malformed_exit_plan_is_refused(raw, code):
    with pytest.raises(ExitPlanInputError) as caught:
        parse_exit_plan_input(raw)
    assert str(caught.value) == code
