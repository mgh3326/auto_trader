"""hk 6148: closed product set, front/next resolution and roll_state (pure)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.kis_futures_quotes import contracts as c

pytestmark = pytest.mark.unit


def _row(ptype: str, short: str, name: str, under: str = "KOSPI200") -> dict[str, str]:
    return {
        "product_type": ptype,
        "short_code": short,
        "standard_code": "KR4X",
        "name": name,
        "atm": "",
        "strike": "00000.00",
        "month_type": "1",
        "underlying_code": "2001",
        "underlying_name": under,
    }


MASTER = [
    _row("B", "A05611", "미니F 202611"),
    _row("B", "A05612", "미니F 202612"),
    _row("B", "A05703", "미니F 202703"),
    _row("B", "A05706", "미니F 202706"),
    _row("1", "A01611", "F 202611"),
    _row("1", "A01612", "F 202612"),
    _row("1", "A01703", "F 202703"),
    # not in the closed set
    _row("2", "201W12260", "C 202612 400.0"),  # option
    _row("B", "A05612", "미니F 202612", under="KOSDAQ150"),  # other underlying
    _row("1", "A01699", "F 202699"),  # month 99
]
TUE = date(2026, 11, 24)


def test_closed_set_is_exactly_kospi200_and_mini() -> None:
    assert [p.code for p in c.CLOSED_QUOTE_PRODUCTS] == ["F200", "MF200"]


def test_front_and_next_are_resolved_from_the_master_not_guessed() -> None:
    contracts, unresolved = c.resolve_front_next(MASTER, TUE)
    assert unresolved == []
    got = {(x.product.code, x.rank): (x.symbol, x.short_code) for x in contracts}
    # 2026-11 expired on 11-12 (second Thursday); 12 is the front, 2027-03 the next
    assert got == {
        ("F200", 0): ("F200.2612", "A01612"),
        ("F200", 1): ("F200.2703", "A01703"),
        ("MF200", 0): ("MF200.2612", "A05612"),
        ("MF200", 1): ("MF200.2703", "A05703"),
    }
    front = next(x for x in contracts if x.symbol == "MF200.2612")
    assert front.contract_month == "2026-12"
    assert front.yymm == "2612"
    assert front.last_trade_date == date(2026, 12, 10)


def test_before_the_second_thursday_the_expiring_month_is_still_front() -> None:
    contracts, _ = c.resolve_front_next(MASTER, date(2026, 11, 12))
    mini = sorted(
        (x for x in contracts if x.product.code == "MF200"), key=lambda x: x.rank
    )
    assert [x.symbol for x in mini] == ["MF200.2611", "MF200.2612"]


def test_a_product_without_a_live_contract_is_unresolved_not_invented() -> None:
    only_regular = [r for r in MASTER if r["product_type"] == "1"]
    contracts, unresolved = c.resolve_front_next(only_regular, TUE)
    assert unresolved == ["MF200"]
    assert {x.product.code for x in contracts} == {"F200"}
    assert c.resolve_front_next([], TUE)[0] == []


def test_short_code_that_disagrees_with_the_name_is_dropped() -> None:
    rows = [_row("B", "A05611", "미니F 202612")]  # year digit ok, month wrong
    assert c.resolve_front_next(rows, TUE, (c.MINI_KOSPI200_FUTURES,))[0] == []


def test_conflicting_duplicate_month_is_dropped_fail_closed() -> None:
    rows = [_row("B", "A05612", "미니F 202612"), _row("B", "A05712", "미니F 202612")]
    assert c.resolve_front_next(rows, TUE, (c.MINI_KOSPI200_FUTURES,))[0] == []


def test_last_trade_date_inside_and_outside_the_known_calendar() -> None:
    assert c.last_trade_date_for("2026-12") == date(2026, 12, 10)
    # outside the tabulated range: plain second-Thursday rule, no holiday adjustment
    assert c.last_trade_date_for("2027-03") == date(2027, 3, 11)
    assert c.last_trade_date_for("2026-11") == date(2026, 11, 12)


LISTED = ["2026-11", "2026-12", "2027-03"]


@pytest.mark.parametrize(
    ("month", "on_date", "expected"),
    [
        ("2026-12", date(2026, 11, 24), "front"),
        ("2027-03", date(2026, 11, 24), "next"),
        # week before the expiry week
        ("2026-12", date(2026, 12, 4), "front"),
        # Monday .. Thursday of the week containing 12-10 (Thursday)
        ("2026-12", date(2026, 12, 7), "expiring"),
        ("2026-12", date(2026, 12, 9), "expiring"),
        ("2026-12", date(2026, 12, 10), "expiring"),
        # the day after the last trade date
        ("2026-12", date(2026, 12, 11), "expired"),
        # the next contract is "next" until the front expires, then "front"
        ("2027-03", date(2026, 12, 10), "next"),
        ("2027-03", date(2026, 12, 11), "front"),
        # the previous cycle: 2026-11 around 11-12
        ("2026-11", date(2026, 11, 9), "expiring"),
        ("2026-11", date(2026, 11, 12), "expiring"),
        ("2026-11", date(2026, 11, 13), "expired"),
        ("2026-11", date(2026, 11, 6), "front"),
    ],
)
def test_roll_state(month: str, on_date: date, expected: str) -> None:
    assert c.roll_state(month, on_date, LISTED) == expected


def test_a_third_contract_is_outside_the_collected_set() -> None:
    with pytest.raises(ValueError):
        c.roll_state("2027-03", date(2026, 11, 6), LISTED)  # 11, 12 both still live


def test_roll_states_constant_matches_the_migration_vocabulary() -> None:
    assert c.ROLL_STATES == ("front", "next", "expiring", "expired")
