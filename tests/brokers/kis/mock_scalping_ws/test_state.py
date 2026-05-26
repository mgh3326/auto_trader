"""Per-symbol MarketState tracker tests (ROB-321 PR2 Task 2)."""

from __future__ import annotations

import pytest

from app.services.brokers.kis.mock_scalping_ws.quote_parsers import (
    OrderBookSnapshot,
    QuoteTick,
)
from app.services.brokers.kis.mock_scalping_ws.state import MarketState


@pytest.mark.unit
def test_tick_updates_last_price_and_ts() -> None:
    state = MarketState(symbol="005930")
    state.update_from_tick(
        QuoteTick(symbol="005930", last_price=70500.0, ts="131502"), now=100.0
    )
    assert state.last_price == 70500.0
    assert state.last_ts == "131502"


@pytest.mark.unit
def test_book_updates_bid_ask_and_spread() -> None:
    state = MarketState(symbol="005930")
    state.update_from_book(
        OrderBookSnapshot(
            symbol="005930", bid=70500.0, ask=70600.0, bid_qty=200.0, ask_qty=120.0
        ),
        now=100.0,
    )
    assert state.bid == 70500.0
    assert state.ask == 70600.0
    # spread_bps = (ask-bid)/mid * 10000 = 100 / 70550 * 10000 ≈ 14.17
    assert state.spread_bps() == pytest.approx(14.17, abs=0.1)


@pytest.mark.unit
def test_spread_none_until_book_seen() -> None:
    state = MarketState(symbol="005930")
    assert state.spread_bps() is None
    state.update_from_tick(
        QuoteTick(symbol="005930", last_price=70500.0, ts="131502"), now=100.0
    )
    # tick alone does not establish a book
    assert state.spread_bps() is None


@pytest.mark.unit
def test_age_seconds_tracks_latest_update() -> None:
    state = MarketState(symbol="005930")
    assert state.age_seconds(now=100.0) is None  # never updated
    state.update_from_tick(
        QuoteTick(symbol="005930", last_price=70500.0, ts="131502"), now=100.0
    )
    assert state.age_seconds(now=102.5) == pytest.approx(2.5)
    # a newer book update resets the freshness clock
    state.update_from_book(
        OrderBookSnapshot(
            symbol="005930", bid=70500.0, ask=70600.0, bid_qty=1.0, ask_qty=1.0
        ),
        now=105.0,
    )
    assert state.age_seconds(now=106.0) == pytest.approx(1.0)


@pytest.mark.unit
def test_spread_none_when_bid_or_ask_nonpositive() -> None:
    state = MarketState(symbol="005930")
    state.update_from_book(
        OrderBookSnapshot(
            symbol="005930", bid=0.0, ask=70600.0, bid_qty=1.0, ask_qty=1.0
        ),
        now=100.0,
    )
    assert state.spread_bps() is None
