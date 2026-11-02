"""ROB-1470 canary statistics per the ROB-1468 design (pure)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from app.services.strategy_sleeves.canary import (
    HorizonMark,
    build_cohort,
    episodes_of,
    safety_state,
    signals_of,
    statistical_verdict,
)
from tests.services.strategy_sleeves.support import NOW, D, fill, order

pytestmark = pytest.mark.unit


def test_rungs_revisions_and_partial_fills_of_one_root_are_one_sample() -> None:
    orders = [
        order(1, signal_key="root-a", signal_minutes=-90),
        order(2, signal_key="root-a", signal_minutes=-30),  # a revision / ladder
        order(3, signal_key="root-b"),
    ]
    fills = [
        fill(1, 1, quantity="4", price="100"),
        fill(2, 1, quantity="6", price="100", minutes=1),  # partial fills
        fill(3, 2, quantity="10", price="110", minutes=2),
    ]
    cohort = build_cohort(
        orders=orders,
        fills=fills,
        marks={
            "root-a": HorizonMark(True, D("120")),
            "root-b": HorizonMark(True, D("120")),
        },
        budget_at={"root-a": D("10000"), "root-b": D("10000")},
    )
    assert cohort.signals == 2
    [a, b] = cohort.samples
    assert a.signal_key == "root-a"
    assert a.signal_at == NOW - timedelta(minutes=90)
    # 10 x (120 - 100) + 10 x (120 - 110)
    assert a.result == D("300")
    assert a.result_pct_of_budget == D("3")
    assert (b.status, b.result) == ("matured_unfilled", D("0"))


def test_unfilled_signals_stay_in_the_cohort_and_in_the_mean() -> None:
    orders = [order(1, signal_key="filled"), order(2, signal_key="unfilled")]
    cohort = build_cohort(
        orders=orders,
        fills=[fill(1, 1, quantity="10", price="100", fee="10")],
        marks={
            "filled": HorizonMark(True, D("110")),
            "unfilled": HorizonMark(True, D("110")),
        },
        budget_at={"filled": D("1000"), "unfilled": D("1000")},
    )
    # filled: 10 x 10 - 10 fee = 90 -> 9%; unfilled: 0% -> mean 4.5%.
    assert cohort.matured == 2
    assert cohort.matured_unfilled == 1
    assert cohort.mean_result_pct_of_budget == D("4.5")


def test_a_sell_signal_is_marked_against_holding_to_h() -> None:
    cohort = build_cohort(
        orders=[order(1, side="sell", signal_key="cut")],
        fills=[fill(1, 1, side="sell", quantity="10", price="80")],
        marks={"cut": HorizonMark(True, D("70"))},
        budget_at={"cut": D("1000")},
    )
    # Sold at 80, the close at H is 70: the cut saved 10 x 10.
    assert cohort.samples[0].result == D("100")


def test_open_and_unmeasured_signals_are_counted_never_averaged_as_zero() -> None:
    orders = [order(1, signal_key="open"), order(2, signal_key="nomark")]
    cohort = build_cohort(
        orders=orders,
        fills=[fill(1, 1), fill(2, 2)],
        marks={"open": HorizonMark(False), "nomark": HorizonMark(True, None)},
        budget_at={"open": D("1000"), "nomark": D("1000")},
    )
    assert (cohort.open, cohort.unmeasured, cohort.matured) == (1, 1, 0)
    assert cohort.mean_result_pct_of_budget is None
    assert all(s.result is None for s in cohort.samples)


def test_the_budget_denominator_is_the_one_in_effect_at_the_signal() -> None:
    cohort = build_cohort(
        orders=[order(1, signal_key="s")],
        fills=[fill(1, 1, quantity="1", price="100")],
        marks={"s": HorizonMark(True, D("150"))},
        budget_at={"s": D("0")},
    )
    assert cohort.samples[0].result == D("50")
    assert cohort.samples[0].result_pct_of_budget is None


def test_orders_without_a_signal_key_are_not_samples() -> None:
    assert signals_of([replace(order(1), signal_key=None)]) == []


@pytest.mark.parametrize(
    ("complete", "first_live", "days", "now_offset", "expected"),
    [
        (False, NOW, 90, 0, "not_preregistered"),
        (True, None, 90, 0, "not_started"),
        (True, NOW, 90, 89, "collecting"),
        (True, NOW, 90, 90, "final_date_reached"),
    ],
)
def test_statistical_verdict(complete, first_live, days, now_offset, expected) -> None:
    verdict, final = statistical_verdict(
        prereg_complete=complete,
        first_live_at=first_live,
        max_duration_days=days,
        now=NOW + timedelta(days=now_offset),
    )
    assert verdict == expected
    if expected in ("collecting", "final_date_reached"):
        assert final == NOW + timedelta(days=90)


@pytest.mark.parametrize(
    ("state", "reached", "left", "expected"),
    [
        ("stopped", False, D("1"), "stopped"),
        ("paused", False, D("1"), "paused"),
        ("shadow", None, None, "shadow"),
        ("live", True, D("1"), "loss_limit_reached"),
        ("live", False, D("-1"), "over_budget"),
        ("live", None, D("1"), "unmeasured"),
        ("live", False, D("0"), "ok"),
    ],
)
def test_safety_state_is_separate_from_the_verdict(
    state, reached, left, expected
) -> None:
    assert (
        safety_state(state=state, loss_limit_reached=reached, budget_left=left)
        == expected
    )


def test_an_add_while_the_position_is_held_joins_the_first_episode() -> None:
    """ROB-1468: adds are not separate samples; a new entry after flat is."""
    orders = [
        order(1, signal_key="entry", signal_minutes=-120),
        order(2, signal_key="add", signal_minutes=-50),
        order(3, side="sell", signal_key="exit", signal_minutes=-30),
        order(4, signal_key="re-entry", signal_minutes=-10),
    ]
    fills = [
        fill(1, 1, quantity="10", price="100", minutes=-100),
        fill(2, 2, quantity="5", price="90", minutes=-40),
        fill(3, 3, side="sell", quantity="15", price="110", minutes=-20),
    ]
    episodes = episodes_of(orders, fills)
    assert [(e.episode_key, e.signal_keys) for e in episodes] == [
        ("entry", ("entry", "add", "exit")),
        ("re-entry", ("re-entry",)),
    ]
    cohort = build_cohort(
        orders=orders,
        fills=fills,
        marks={
            "entry": HorizonMark(True, D("120")),
            "re-entry": HorizonMark(False),
        },
        budget_at={"entry": D("1000"), "re-entry": D("1000")},
    )
    assert (cohort.signals, cohort.episodes) == (4, 2)
    [entry, re_entry] = cohort.samples
    # 10 x 20 + 5 x 30 - 15 x 10 = 200 (the exit realized 110 against 120 at H)
    assert entry.result == D("200")
    assert entry.signals == 3
    assert re_entry.status == "open"
