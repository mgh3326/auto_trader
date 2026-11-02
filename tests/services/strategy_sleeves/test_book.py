"""ROB-1470 pure sleeve book: lots, P&L and cash used."""

from __future__ import annotations

import pytest

from app.services.strategy_sleeves.book import compute_sleeve_book
from tests.services.strategy_sleeves.support import D, fill, order

pytestmark = pytest.mark.unit


def test_fifo_lots_realized_and_unrealized_pnl() -> None:
    orders = [order(1), order(2, limit_price="120"), order(3, side="sell")]
    fills = [
        fill(1, 1, quantity="10", price="100", minutes=0),
        fill(2, 2, quantity="10", price="120", minutes=1),
        fill(3, 3, side="sell", quantity="15", price="130", fee="2", minutes=2),
    ]
    book = compute_sleeve_book(orders=orders, fills=fills, marks={"AAA": D("110")})

    # FIFO: 10 @ 100 and 5 @ 120 sold at 130.
    assert book.realized_pnl == D("10") * 30 + D("5") * 10
    [position] = book.positions
    assert (position.quantity, position.cost_basis) == (D("5"), D("600"))
    assert book.unrealized_pnl == D("5") * 110 - D("600")
    assert book.fees == D("2")
    assert book.net_pnl == D("350") - D("50") - D("2")
    assert book.held_cost_basis == D("600")
    assert book.used == D("600")
    assert book.trades == 3


def test_a_missing_mark_makes_pnl_unknown_not_zero() -> None:
    book = compute_sleeve_book(
        orders=[order(1)], fills=[fill(1, 1)], marks={"AAA": None}
    )
    assert book.unrealized_pnl is None
    assert book.net_pnl is None
    assert book.loss is None
    assert book.missing_marks == ("AAA",)


def test_open_buy_orders_count_their_unfilled_remainder_once() -> None:
    orders = [
        order(1, state="partially_filled", quantity="10", limit_price="100"),
        order(2, state="resting", quantity="4", limit_price="50"),
        order(3, state="pending_approval", quantity="99", limit_price="100"),
        order(4, state="resting", side="sell", quantity="3", limit_price="200"),
    ]
    book = compute_sleeve_book(
        orders=orders, fills=[fill(1, 1, quantity="6")], marks={"AAA": D("100")}
    )
    # held 6 @ 100 + open (10-6) @ 100 + open 4 @ 50; pending cards and sells
    # are not open buy orders.
    assert book.held_cost_basis == D("600")
    assert book.open_order_notional == D("400") + D("200")
    assert book.used == D("1200")


def test_a_gated_proposals_rung_at_the_broker_still_counts_once() -> None:
    """Its acked rung is an open order; its pending rung is the gated order."""
    orders = [
        order(1, proposal_pk=11, state="acked", quantity="10", limit_price="100"),
        order(2, proposal_pk=11, state="pending_approval", quantity="5"),
    ]
    book = compute_sleeve_book(orders=orders, fills=[], marks={})
    assert book.open_order_notional == D("1000")
    assert book.used == D("1000")


def test_a_filled_rung_with_no_quantity_and_no_booked_fill_is_unmeasured() -> None:
    book = compute_sleeve_book(
        orders=[order(1, state="filled", rung_filled_qty=None)], fills=[], marks={}
    )
    assert book.used is None
    assert book.unmeasured_orders == (1,)


@pytest.mark.parametrize("state", ["cancelled", "expired"])
def test_a_cancelled_or_expired_rung_with_no_quantity_uses_nothing(state) -> None:
    book = compute_sleeve_book(
        orders=[order(1, state=state, rung_filled_qty=None)], fills=[], marks={}
    )
    assert book.used == D("0")
    assert book.unmeasured_orders == ()


def test_an_unattributed_fill_makes_pnl_unknown_not_zero() -> None:
    book = compute_sleeve_book(
        orders=[order(1, state="resting")],
        fills=[],
        marks={},
        unattributed_rungs=[1],
    )
    assert book.net_pnl is None
    assert book.loss is None
    assert book.unattributed_rungs == (1,)


def test_a_fill_the_rung_reports_but_the_ledger_has_not_booked_still_counts() -> None:
    orders = [order(1, state="filled", quantity="10", rung_filled_qty="10")]
    book = compute_sleeve_book(
        orders=orders, fills=[fill(1, 1, quantity="4")], marks={"AAA": D("100")}
    )
    assert book.unbooked_fill_notional == D("600")
    assert book.used == D("400") + D("600")


def test_an_unpriced_open_order_makes_used_unknown() -> None:
    orders = [order(1, state="resting", limit_price=None, notional=None)]
    book = compute_sleeve_book(orders=orders, fills=[], marks={})
    assert book.used is None
    assert book.unmeasured_orders == (1,)


def test_a_sell_without_sleeve_cost_basis_adds_no_realized_pnl() -> None:
    book = compute_sleeve_book(
        orders=[order(1, side="sell")],
        fills=[fill(1, 1, side="sell", quantity="5", price="90")],
        marks={},
    )
    assert book.realized_pnl == D("0")
    assert book.unmatched_sell_quantity == D("5")
    assert book.net_pnl == D("0")


def test_two_sleeves_books_are_independent_of_each_others_fills() -> None:
    """Acceptance 3 (pure half): a book only folds the fills it is given."""
    sleeve_a = compute_sleeve_book(
        orders=[order(1)], fills=[fill(1, 1)], marks={"AAA": D("90")}
    )
    sleeve_b_before = compute_sleeve_book(orders=[order(2)], fills=[], marks={})
    sleeve_b_after = compute_sleeve_book(orders=[order(2)], fills=[], marks={})
    assert sleeve_a.net_pnl == D("-100")
    assert sleeve_b_before == sleeve_b_after
    assert sleeve_b_after.positions == ()
