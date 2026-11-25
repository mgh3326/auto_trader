"""hk 6498: pure KR price-kind selection (no I/O)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.services import kr_quote_price as kqp

KST = kqp.KST


def _kst(hour: int, minute: int = 0, day: int = 25) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=KST)


# 2026-11-25 after-hours case from the hk 6039 evidence (005930).
AFTER_NOW = _kst(18, 16)
AFTER_SESSION_START = _kst(15, 30)


def _nxt_book(
    *,
    bid: float | None = 169100.0,
    ask: float | None = 169200.0,
    expected: float | None = 170800.0,
    observed: dt.datetime | None = _kst(18, 16),
) -> kqp.VenueBook:
    return kqp.VenueBook(
        venue="nxt",
        best_bid=bid,
        best_ask=ask,
        observed_at=observed,
        expected_price=expected,
    )


def _trade(price: float, at: dt.datetime, venue: str = "nxt") -> kqp.LastTrade:
    return kqp.LastTrade(venue=venue, price=price, traded_at=at)


def test_after_hours_uses_last_trade_and_keeps_expected_separately():
    """Acceptance 1: 18:11 trade 169,400 is the price; expected 170,800 is kept."""
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_NXT_POST,
        session_start=AFTER_SESSION_START,
        now=AFTER_NOW,
        venue_books={"nxt": _nxt_book()},
        last_trades={"nxt": _trade(169400.0, _kst(18, 11))},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_LAST_TRADE
    assert sel.price == 169400.0
    assert sel.price_session == kqp.PRICE_SESSION_NXT_POST
    assert sel.price_as_of == _kst(18, 11)
    assert sel.expected_price == 170800.0
    assert sel.expected_price_venue == "nxt"
    assert sel.reason is None
    assert sel.venues["nxt"]["best_bid"] == 169100.0
    assert sel.venues["nxt"]["best_ask"] == 169200.0
    assert sel.venues["nxt"]["observed_at"] == _kst(18, 16).isoformat()
    assert sel.venues["nxt"]["last_trade_price"] == 169400.0


def test_after_hours_without_trade_uses_firm_mid_169150():
    """Acceptance 1, no-trade branch: the two-sided mid 169,150, not expected."""
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_NXT_POST,
        session_start=AFTER_SESSION_START,
        now=AFTER_NOW,
        venue_books={"nxt": _nxt_book()},
        last_trades={},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_MID
    assert sel.price == pytest.approx(169150.0)
    assert sel.expected_price == 170800.0
    assert sel.price_as_of == _kst(18, 16)


def test_trade_before_session_start_does_not_count():
    """A trade before the session window is not a trade in that session."""
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_NXT_POST,
        session_start=AFTER_SESSION_START,
        now=AFTER_NOW,
        venue_books={"nxt": _nxt_book()},
        last_trades={"nxt": _trade(169000.0, _kst(9, 5))},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_MID
    assert sel.price == pytest.approx(169150.0)


def test_trade_after_now_does_not_count():
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_NXT_POST,
        session_start=AFTER_SESSION_START,
        now=AFTER_NOW,
        venue_books={"nxt": _nxt_book()},
        last_trades={"nxt": _trade(169400.0, _kst(18, 30))},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_MID


def test_continuous_expected_only_is_never_the_price():
    """Continuous session + expected but no two-sided book: unavailable, not expected."""
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_NXT_PRE,
        session_start=_kst(8, 0),
        now=_kst(8, 20),
        venue_books={"nxt": _nxt_book(bid=None, ask=None, expected=170800.0)},
        last_trades={},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_UNAVAILABLE
    assert sel.price is None
    assert sel.expected_price == 170800.0
    assert sel.reason == kqp.REASON_NO_TRADE_NO_BOOK


def test_one_sided_book_is_never_a_price():
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_NXT_POST,
        session_start=AFTER_SESSION_START,
        now=AFTER_NOW,
        venue_books={"nxt": _nxt_book(bid=None, ask=169200.0, expected=None)},
        last_trades={},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_UNAVAILABLE
    assert sel.price is None
    assert sel.reason == kqp.REASON_NO_TRADE_ONE_SIDED


def test_session_unknown_is_unavailable_with_reason_and_never_guesses():
    sel = kqp.select_kr_price(
        session=None,
        session_start=None,
        now=AFTER_NOW,
        venue_books={"nxt": _nxt_book()},
        last_trades={"nxt": _trade(169400.0, _kst(18, 11))},
        primary_venue="nxt",
    )

    assert sel.price_kind == kqp.PRICE_KIND_UNAVAILABLE
    assert sel.price is None
    assert sel.reason == kqp.REASON_SESSION_UNKNOWN
    assert sel.expected_price == 170800.0


def test_auction_phase_expected_is_the_price_but_marked_expected():
    """Acceptance 2 input: a KRX closing-auction book gives price_kind=expected."""
    krx_book = kqp.VenueBook(
        venue="krx",
        best_bid=170600.0,
        best_ask=170700.0,
        observed_at=_kst(15, 25),
        expected_price=170700.0,
    )
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_AUCTION,
        session_start=None,
        now=_kst(15, 25),
        venue_books={"krx": krx_book},
        last_trades={},
        primary_venue="krx",
    )

    assert sel.price_kind == kqp.PRICE_KIND_EXPECTED
    assert sel.price == 170700.0
    assert sel.expected_price == 170700.0
    assert sel.expected_price_venue == "krx"
    assert sel.reason == kqp.REASON_EXPECTED_NOT_ORDER_BASIS


def test_auction_without_expected_is_unavailable():
    krx_book = kqp.VenueBook(
        venue="krx",
        best_bid=170600.0,
        best_ask=170700.0,
        observed_at=_kst(15, 25),
        expected_price=None,
    )
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_AUCTION,
        session_start=None,
        now=_kst(15, 25),
        venue_books={"krx": krx_book},
        last_trades={},
        primary_venue="krx",
    )

    assert sel.price_kind == kqp.PRICE_KIND_UNAVAILABLE
    assert sel.reason == kqp.REASON_AUCTION_WITHOUT_EXPECTED


def test_krx_after_session_uses_krx_book_and_last_trade():
    """KRX venue in the KRX after window: same rule, its own venue fields."""
    krx_book = kqp.VenueBook(
        venue="krx",
        best_bid=169100.0,
        best_ask=169200.0,
        observed_at=AFTER_NOW,
        expected_price=170700.0,
    )
    sel = kqp.select_kr_price(
        session=kqp.PRICE_SESSION_KRX_AFTER,
        session_start=AFTER_SESSION_START,
        now=AFTER_NOW,
        venue_books={"krx": krx_book},
        last_trades={"krx": _trade(169400.0, _kst(18, 11), venue="krx")},
        primary_venue="krx",
    )

    assert sel.price_kind == kqp.PRICE_KIND_LAST_TRADE
    assert sel.price == 169400.0
    assert sel.price_session == kqp.PRICE_SESSION_KRX_AFTER
    assert sel.expected_price == 170700.0
    assert sel.venues["krx"]["expected_price"] == 170700.0


def test_nxt_session_mapping():
    assert kqp.nxt_price_session("nxt_premarket") == (
        kqp.PRICE_SESSION_NXT_PRE,
        kqp.NXT_PRE_START,
    )
    assert kqp.nxt_price_session("nxt_after") == (
        kqp.PRICE_SESSION_NXT_POST,
        kqp.NXT_POST_START,
    )
    assert kqp.nxt_price_session("regular") == (None, None)
    assert kqp.nxt_price_session(None) == (None, None)


def test_in_krx_auction_windows():
    assert kqp.in_krx_auction(_kst(8, 30))
    assert kqp.in_krx_auction(_kst(15, 25))
    assert not kqp.in_krx_auction(_kst(9, 0))
    assert not kqp.in_krx_auction(_kst(12, 0))
    assert not kqp.in_krx_auction(dt.datetime(2026, 11, 25, 8, 30))  # naive


@pytest.mark.parametrize("value", [None, "", "abc", True, 0, -1, float("nan")])
def test_positive_price_rejects_non_positive_and_non_numeric(value):
    assert kqp.positive_price(value) is None


def test_positive_price_accepts_numeric_strings_and_floats():
    assert kqp.positive_price("169400") == 169400.0
    assert kqp.positive_price(169400) == 169400.0
