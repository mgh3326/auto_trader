"""Pure invariant functions (I2, I3, I4, I5, I6, I15, I17) with the design's own
fixtures (§3.2 "회차 기준값", §3.8). No database."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services.mandate import vocabulary as v
from app.services.mandate.invariants import (
    InvariantViolation,
    PlanAmounts,
    account_binding_ok,
    check_one_share,
    is_parking_symbol,
    min_gap_met,
    sector_cap_check,
    sleeve_budget_check,
    symbol_allowed,
    tranche_amounts,
    tranche_order_ok,
    tranche_trigger_met,
)
from tests.services.mandate.support import Stream

pytestmark = pytest.mark.unit
D = Decimal
MAN = D(10_000)  # 1 man = 10,000 won; the design table is in man


def man(n: str) -> Decimal:
    return D(n) * MAN


# ------------------------------------------------------------------ I5 triggers
@pytest.mark.parametrize(
    ("price", "pct", "met"),
    [
        # tranche 2 (-15%), first fill 888,600 / 9 shares
        ("83923", "-15", True),
        ("83924", "-15", False),
        # tranche 3 (-30%)
        ("69113", "-30", True),
        ("69114", "-30", False),
    ],
)
def test_i5_trigger_boundary_is_division_free_and_inclusive(price, pct, met) -> None:
    assert (
        tranche_trigger_met(D(price), D("888600"), D("9"), D(pct)) is met
    )


def test_i5_no_trigger_without_a_first_fill_or_a_price() -> None:
    assert not tranche_trigger_met(D("1"), D("0"), D("0"), D("-15"))
    assert not tranche_trigger_met(D("0"), D("888600"), D("9"), D("-15"))
    assert not tranche_trigger_met(D("NaN"), D("888600"), D("9"), D("-15"))


# ---------------------------------------------------------------------- I6 gap
def _sessions(start: date, n: int) -> list[date]:
    out, day = [], start
    while len(out) < n:
        day += timedelta(days=1)
        if day.weekday() < 5:
            out.append(day)
    return out


def test_i6_twenty_sessions_after_the_settle_day() -> None:
    settled = date(2026, 11, 24)
    sessions = _sessions(settled, 25)
    assert not min_gap_met(settled, sessions[18], sessions, 20)  # 19 sessions
    assert min_gap_met(settled, sessions[19], sessions, 20)  # the 20th
    assert not min_gap_met(None, sessions[24], sessions, 20)  # no fill, no gap


# ---------------------------------------------------------------------- I7 order
def test_i7_order_reads_the_fold() -> None:
    s = Stream()
    s.approve()
    assert tranche_order_ok(s.state, 1)
    assert not tranche_order_ok(s.state, 2)
    s.fill_first()
    assert tranche_order_ok(s.state, 2)


# ---------------------------------------------------------------- tranche amounts
def test_tranche_amounts_split_only_the_new_money() -> None:
    amounts = tranche_amounts(man("235"), man("100"), ["0.4", "0.3", "0.3"])
    assert amounts == (man("54"), man("40.5"), man("40.5"))
    assert sum(amounts) == man("135")


def test_tranche_amounts_refuse_a_split_that_does_not_add_up() -> None:
    with pytest.raises(InvariantViolation):
        tranche_amounts(D("100.0001"), D("0"), ["0.4", "0.3", "0.3"])  # 5 decimals
    with pytest.raises(InvariantViolation):
        tranche_amounts(D("100"), D("0"), ["0.5", "0.3", "0.3"])  # weights != 1
    with pytest.raises(InvariantViolation):
        tranche_amounts(D("100"), D("200"), ["0.4", "0.3", "0.3"])  # adopted > planned


def test_every_tranche_must_afford_one_share() -> None:
    check_one_share((D("940000"),), D("100000"))
    with pytest.raises(InvariantViolation) as caught:
        check_one_share((D("940000"), D("99999")), D("100000"))
    assert caught.value.code is v.MandateRefusal.TRANCHE_BELOW_ONE_SHARE
    with pytest.raises(InvariantViolation):
        check_one_share((D("940000"),), D("0"))  # no price: no registration


# ---------------------------------------------------------------------- I3 budget
def test_i3_sleeve_budget_counts_new_money_only() -> None:
    cap = man("1000")
    x = PlanAmounts(man("235"), man("100"), "auto")  # N = 135
    z = PlanAmounts(man("235"), man("0"), "auto")  # N = 235
    sleeve_budget_check(z, [x], cap)  # 370 <= 1000
    with pytest.raises(InvariantViolation) as caught:
        sleeve_budget_check(PlanAmounts(man("900"), D("0"), "x"), [x, z], cap)
    assert caught.value.code is v.MandateRefusal.BUDGET_EXCEEDED


# ---------------------------------------------------------------------- I4 sector
def _basket() -> tuple[list[PlanAmounts], tuple[Decimal, Decimal]]:
    plans = [
        PlanAmounts(man("235"), man("100"), "auto"),  # X (Toss)
        PlanAmounts(man("235"), man("100"), "auto"),  # Y (KIS)
        PlanAmounts(man("235"), man("0"), "auto"),  # Z
        PlanAmounts(man("235"), man("60"), "fin"),  # W
    ]
    return plans, (man("1000"), man("950"))


def test_i4_design_example_three_pass_and_a_fourth_auto_plan_is_refused() -> None:
    plans, caps = _basket()
    # the first three automotive plans: numerator 705 <= 35% x 2,210 (773.5)
    sector_cap_check(plans[2], plans[:2] + plans[3:], caps, D("35"))
    # a fourth automotive plan: numerator 940 > 773.5
    fourth = PlanAmounts(man("235"), man("0"), "auto")
    with pytest.raises(InvariantViolation) as caught:
        sector_cap_check(fourth, plans, caps, D("35"))
    assert caught.value.code is v.MandateRefusal.SECTOR_CAP


def test_i4_adopted_amounts_enter_the_numerator_through_planned_once() -> None:
    """Dropping A from the numerator would let the fourth plan pass (505 < 773.5)."""
    plans, caps = _basket()
    fourth = PlanAmounts(man("235"), man("0"), "auto")
    wrong_numerator = sum(p.new_amount for p in [*plans[:3], fourth])
    assert wrong_numerator * 100 <= man("2210") * 35  # the mistake would pass
    with pytest.raises(InvariantViolation):
        sector_cap_check(fourth, plans, caps, D("35"))


def test_i4_a_released_plan_no_longer_counts_but_a_retired_one_still_does() -> None:
    plans, caps = _basket()
    fourth = PlanAmounts(man("235"), man("0"), "auto")
    # the caller passes only unreleased plans: a retired (unreleased) Y still counts
    with pytest.raises(InvariantViolation):
        sector_cap_check(fourth, plans, caps, D("35"))
    # once Y is released (left out), the denominator loses its A (100) as well
    without_y = [plans[0], plans[2], plans[3]]
    sector_cap_check(fourth, without_y, caps, D("35"))


# ------------------------------------------------------------------ I17 / I2 / I15
@pytest.mark.parametrize("symbol", ["459580", "357870", "SGOV", "BIL", " sgov "])
def test_i17_parking_symbols_are_not_plan_targets(symbol) -> None:
    assert is_parking_symbol(symbol)


@pytest.mark.parametrize("symbol", ["005930", "BRK.B", "AAPL"])
def test_i17_ordinary_symbols_pass(symbol) -> None:
    assert not is_parking_symbol(symbol)


def test_i17_an_unreadable_symbol_fails_closed() -> None:
    assert is_parking_symbol("한글")


def test_i2_symbol_must_be_listed() -> None:
    assert symbol_allowed("005930", ["005930", "000270"])
    assert not symbol_allowed("005380", ["005930", "000270"])


def test_i15_plan_account_is_the_clause_account_and_a_scope_account() -> None:
    ok = dict(terms_account_ref="t", plan_account_ref="t", scope_account_refs=["t", "k"])
    assert account_binding_ok(**ok)
    assert not account_binding_ok(**{**ok, "plan_account_ref": "k"})
    assert not account_binding_ok(**{**ok, "scope_account_refs": ["k"]})
    assert not account_binding_ok(**{**ok, "scope_account_refs": []})
    assert not account_binding_ok(**{**ok, "scope_account_refs": ["t", "t"]})
    assert not account_binding_ok(**{**ok, "scope_account_refs": ["t", "k", "x"]})
