"""ROB-317 — per-symbol market state + freshness guard."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.demo_scalping_ws.state import MarketState

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def test_new_state_has_no_events_and_is_stale() -> None:
    state = MarketState(symbol="XRPUSDT")
    assert state.last_event_at() is None
    assert state.is_stale(now=_NOW, max_age_seconds=120) is True


def test_last_event_at_is_max_across_streams() -> None:
    state = MarketState(symbol="XRPUSDT")
    state.book_ticker_at = _NOW - dt.timedelta(seconds=10)
    state.agg_trade_at = _NOW - dt.timedelta(seconds=3)
    assert state.last_event_at() == _NOW - dt.timedelta(seconds=3)


def test_fresh_within_max_age() -> None:
    state = MarketState(symbol="XRPUSDT", bid_price=Decimal("0.5"))
    state.agg_trade_at = _NOW - dt.timedelta(seconds=30)
    assert state.is_stale(now=_NOW, max_age_seconds=120) is False


def test_stale_beyond_max_age() -> None:
    state = MarketState(symbol="XRPUSDT")
    state.agg_trade_at = _NOW - dt.timedelta(seconds=200)
    assert state.is_stale(now=_NOW, max_age_seconds=120) is True


def test_book_data_age_none_without_bookticker() -> None:
    state = MarketState(symbol="XRPUSDT")
    assert state.book_data_age_seconds(now=_NOW) is None


def test_book_data_age_ignores_aggtrade() -> None:
    # A fresh aggTrade must NOT make bookTicker look fresh.
    state = MarketState(symbol="XRPUSDT")
    state.agg_trade_at = _NOW - dt.timedelta(seconds=2)
    assert state.book_data_age_seconds(now=_NOW) is None


def test_book_data_age_from_bookticker() -> None:
    state = MarketState(symbol="XRPUSDT")
    state.book_ticker_at = _NOW - dt.timedelta(seconds=10)
    assert state.book_data_age_seconds(now=_NOW) == 10.0
