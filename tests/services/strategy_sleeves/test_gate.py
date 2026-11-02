"""ROB-1470 pure sleeve gate verdicts."""

from __future__ import annotations

import pytest

from app.services.strategy_sleeves.book import compute_sleeve_book
from app.services.strategy_sleeves.gate import SleeveVersionView, evaluate_sleeve_gate
from app.services.strategy_sleeves.vocabulary import SleeveGateReason
from tests.services.strategy_sleeves.support import D, fill, order

pytestmark = pytest.mark.unit


def _view(state: str = "live", budget: str = "1000", limit: str = "100"):
    return SleeveVersionView(
        sleeve_id=7, version=3, state=state, budget_cap=D(budget), loss_limit=D(limit)
    )


def _book(mark: str | None = "100", held_price: str = "100"):
    return compute_sleeve_book(
        orders=[order(1)],
        fills=[fill(1, 1, quantity="5", price=held_price)],
        marks={"AAA": None if mark is None else D(mark)},
    )


@pytest.mark.parametrize("state", ["shadow", "paused", "stopped"])
def test_a_sleeve_that_is_not_live_sends_every_order_to_a_person(state) -> None:
    for places_buy in (True, False):
        verdict = evaluate_sleeve_gate(
            version=_view(state),
            book=_book(),
            order_buy_notional=D("1"),
            places_buy=places_buy,
        )
        assert (verdict.allowed, verdict.reason) == (False, SleeveGateReason.NOT_LIVE)
        assert verdict.details["sleeve_state"] == state
        assert verdict.stop_sleeve is False


def test_loss_at_the_limit_stops_the_sleeve() -> None:
    # 5 held @ 100, marked 80 -> unrealized -100 == loss limit 100.
    verdict = evaluate_sleeve_gate(
        version=_view(),
        book=_book(mark="80"),
        order_buy_notional=D("1"),
        places_buy=True,
    )
    assert verdict.reason is SleeveGateReason.LOSS_LIMIT_REACHED
    assert verdict.stop_sleeve is True
    assert verdict.details["sleeve_net_pnl"] == "-100"
    assert verdict.details["sleeve_loss_limit"] == "100"


def test_loss_one_unit_under_the_limit_passes() -> None:
    verdict = evaluate_sleeve_gate(
        version=_view(limit="100.0001"),
        book=_book(mark="80"),
        order_buy_notional=D("1"),
        places_buy=True,
    )
    assert verdict.allowed is True


def test_the_loss_limit_also_holds_for_a_sell() -> None:
    verdict = evaluate_sleeve_gate(
        version=_view(),
        book=_book(mark="80"),
        order_buy_notional=D("0"),
        places_buy=False,
    )
    assert verdict.reason is SleeveGateReason.LOSS_LIMIT_REACHED


def test_unknown_pnl_never_passes_as_zero() -> None:
    verdict = evaluate_sleeve_gate(
        version=_view(),
        book=_book(mark=None),
        order_buy_notional=D("1"),
        places_buy=True,
    )
    assert verdict.reason is SleeveGateReason.PNL_UNMEASURED
    assert verdict.details["sleeve_missing_mark_count"] == "1"


def test_over_budget_names_used_order_and_cap() -> None:
    # used 500 (5 @ 100 held) + order 501 = 1001 > 1000.
    verdict = evaluate_sleeve_gate(
        version=_view(), book=_book(), order_buy_notional=D("501"), places_buy=True
    )
    assert verdict.reason is SleeveGateReason.BUDGET_EXCEEDED
    assert verdict.details["sleeve_used"] == "500"
    assert verdict.details["sleeve_order_notional"] == "501"
    assert verdict.details["sleeve_used_after"] == "1001"
    assert verdict.details["sleeve_budget_cap"] == "1000"


def test_exactly_at_budget_passes() -> None:
    verdict = evaluate_sleeve_gate(
        version=_view(), book=_book(), order_buy_notional=D("500"), places_buy=True
    )
    assert verdict.allowed is True


def test_a_sell_spends_no_budget() -> None:
    verdict = evaluate_sleeve_gate(
        version=_view(budget="0"),
        book=_book(),
        order_buy_notional=D("0"),
        places_buy=False,
    )
    assert verdict.allowed is True


def test_unknown_used_or_order_notional_goes_to_a_person() -> None:
    unpriced = compute_sleeve_book(
        orders=[order(9, state="resting", limit_price=None)], fills=[], marks={}
    )
    for book, notional in ((unpriced, D("1")), (_book(), None)):
        verdict = evaluate_sleeve_gate(
            version=_view(), book=book, order_buy_notional=notional, places_buy=True
        )
        assert verdict.reason is SleeveGateReason.USED_UNMEASURED


def test_no_version_or_no_book_is_unreadable() -> None:
    assert (
        evaluate_sleeve_gate(
            version=None, book=_book(), order_buy_notional=D("1"), places_buy=True
        ).reason
        is SleeveGateReason.UNREADABLE
    )
    assert (
        evaluate_sleeve_gate(
            version=_view(), book=None, order_buy_notional=D("1"), places_buy=True
        ).reason
        is SleeveGateReason.UNREADABLE
    )
