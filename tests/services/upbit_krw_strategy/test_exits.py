from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.upbit_tick import is_on_upbit_krw_tick
from app.services.upbit_krw_strategy import exits as e

D = Decimal


@pytest.mark.parametrize(
    ("price", "expected"),
    [
        ("1259.19", "1260"),
        ("50979.6", "51000"),
        ("99960", "100000"),  # lands exactly on the next range's first price
        ("100980", "101000"),
        ("999999.024", "1000000"),  # 500 tick range ceils into the 1000 tick range
        ("1000", "1000"),  # already on tick: unchanged
        ("0.0123", "0.0123"),
        ("5.001", "5.01"),
    ],
)
def test_ceil_to_tick_golden(price: str, expected: str) -> None:
    result = e.ceil_to_tick(D(price))
    assert result == D(expected)
    assert is_on_upbit_krw_tick(result)


def test_ceil_to_tick_rejects_float_and_non_positive() -> None:
    for bad in (1.5, D(0), D(-1), D("NaN")):
        with pytest.raises(ValueError):
            e.ceil_to_tick(bad)  # type: ignore[arg-type]


LADDER_GOLDEN = [
    # avg entry, expected rung prices (+2%, +4%, +6%)
    ("1000", ["1020", "1040", "1060"]),
    ("1234.5", ["1260", "1284", "1309"]),
    ("49980", ["51000", "52000", "53000"]),
    ("98000", ["100000", "102000", "103900"]),
    ("980391.2", ["1000000", "1020000", "1040000"]),
]


@pytest.mark.parametrize(("avg", "prices"), LADDER_GOLDEN)
def test_take_profit_ladder_prices_golden(avg: str, prices: list[str]) -> None:
    rungs = e.take_profit_ladder(D(avg), D("3"))
    assert [r.price_krw for r in rungs] == [D(p) for p in prices]
    assert [r.take_profit_return for r in rungs] == [D("0.02"), D("0.04"), D("0.06")]
    assert all(r.price_krw >= D(avg) * (1 + r.take_profit_return) for r in rungs)


def test_ladder_quantities_split_in_thirds_and_sum_exactly() -> None:
    rungs = e.take_profit_ladder(D("1000"), D("1"))
    assert [r.quantity for r in rungs] == [
        D("0.33333333"), D("0.33333333"), D("0.33333334"),
    ]  # fmt: skip
    assert sum(r.quantity for r in rungs) == D("1")
    odd = e.take_profit_ladder(D("1000"), D("0.00000010"))
    assert [r.quantity for r in odd] == [
        D("0.00000003"),
        D("0.00000003"),
        D("0.00000004"),
    ]


def test_ladder_flags_a_rung_below_the_minimum_order() -> None:
    small = e.take_profit_ladder(D("1000"), D("9"))  # 3 x 1020 KRW
    assert all(r.below_min_order for r in small)
    big = e.take_profit_ladder(D("1000"), D("300"))
    assert not any(r.below_min_order for r in big)


def test_ladder_rejects_bad_inputs() -> None:
    with pytest.raises(ValueError):
        e.take_profit_ladder(D(0), D(1))
    with pytest.raises(ValueError):
        e.take_profit_ladder(D(1000), D(0))


def test_hard_stop_price_is_exactly_ten_percent_below() -> None:
    assert e.hard_stop_price(D("1000")) == D("900")
    assert e.hard_stop_price(D("1234.5")) == D("1111.05")


def decide(**kw):
    base = {
        "avg_entry_price": D("1000"),
        "best_bid_price": D("1000"),
        "is_decision_slot": False,
        "r60": D("0.1"),
        "decision_slots_since_fill": 0,
    }
    base.update(kw)
    return e.evaluate_exit(**base)


def test_hard_stop_at_or_below_the_stop_bid() -> None:
    hit = decide(best_bid_price=D("900"))
    assert (hit.exit, hit.reason) == (True, "hard_stop")
    assert not decide(best_bid_price=D("900.0001")).exit


def test_unknown_bid_never_stops_out_and_is_reported() -> None:
    d = decide(best_bid_price=None)
    assert (d.exit, d.hard_stop_unverified) == (False, True)


def test_hard_stop_outranks_trend_exit_and_time_stop() -> None:
    d = decide(
        best_bid_price=D("800"), is_decision_slot=True, r60=D("-1"),
        decision_slots_since_fill=30,
    )  # fmt: skip
    assert d.reason == "hard_stop"


def test_trend_exit_only_at_a_decision_slot_and_outranks_time_stop() -> None:
    assert not decide(r60=D("-0.2"), is_decision_slot=False).exit
    at_decision = decide(r60=D("0"), is_decision_slot=True)  # R60 <= 0 exits
    assert at_decision.reason == "trend_exit"
    both = decide(r60=D("-0.2"), is_decision_slot=True, decision_slots_since_fill=45)
    assert both.reason == "trend_exit"


def test_time_stop_after_30_decision_slots_only_at_a_decision_slot() -> None:
    assert not decide(is_decision_slot=True, decision_slots_since_fill=29).exit
    hit = decide(is_decision_slot=True, decision_slots_since_fill=30)
    assert (hit.exit, hit.reason) == (True, "time_stop")
    assert not decide(is_decision_slot=False, decision_slots_since_fill=99).exit


def test_unknown_r60_at_a_decision_slot_is_unverified_not_an_exit() -> None:
    d = decide(is_decision_slot=True, r60=None)
    assert (d.exit, d.trend_unverified) == (False, True)
    still_time = decide(is_decision_slot=True, r60=None, decision_slots_since_fill=30)
    assert (still_time.reason, still_time.trend_unverified) == ("time_stop", True)


OPENED = datetime(2026, 11, 22, 0, 10, tzinfo=UTC)


@pytest.mark.parametrize(
    ("elapsed", "cycle"),
    [
        (timedelta(0), 0),
        (timedelta(hours=11, minutes=59), 0),
        (timedelta(hours=12), 1),
        (timedelta(hours=119, minutes=59), 9),
        (timedelta(hours=120), None),  # 5 days: the ladder is closed
        (timedelta(days=9), None),
    ],
)
def test_ladder_cycle_is_ten_twelve_hour_renewals(elapsed, cycle) -> None:
    result = e.ladder_cycle(ladder_opened_at=OPENED, now=OPENED + elapsed)
    if cycle is None:
        assert result is None
    else:
        assert result is not None and result.cycle == cycle
        assert result.expires_at - result.opens_at == timedelta(hours=12)
        assert result.opens_at <= OPENED + elapsed < result.expires_at


def test_ladder_cycle_needs_aware_datetimes_and_forward_time() -> None:
    with pytest.raises(ValueError):
        e.ladder_cycle(ladder_opened_at=datetime(2026, 11, 22), now=OPENED)
    with pytest.raises(ValueError):
        e.ladder_cycle(ladder_opened_at=OPENED, now=OPENED - timedelta(seconds=1))


def test_next_open_rung_is_the_lowest_unfilled() -> None:
    assert e.next_open_rung(frozenset()) == 0
    assert e.next_open_rung(frozenset({0})) == 1
    assert e.next_open_rung(frozenset({0, 1})) == 2
    assert e.next_open_rung(frozenset({0, 1, 2})) is None


@pytest.mark.parametrize(
    ("reason", "exit_price", "ban"),
    [
        ("hard_stop", "1000", True),  # a stop always bans
        ("time_stop", "955", True),  # exactly -4.5%
        ("time_stop", "955.1", False),
        ("trend_exit", "900", True),
        ("trend_exit", "1010", False),
    ],
)
def test_reentry_ban_trigger(reason, exit_price, ban) -> None:
    assert (
        e.triggers_reentry_ban(
            reason=reason, avg_entry_price=D("1000"), exit_avg_price=D(exit_price)
        )
        is ban
    )


def test_reentry_ban_lasts_eight_days() -> None:
    assert e.reentry_banned_until(OPENED) == OPENED + timedelta(days=8)
    with pytest.raises(ValueError):
        e.reentry_banned_until(datetime(2026, 11, 22))
