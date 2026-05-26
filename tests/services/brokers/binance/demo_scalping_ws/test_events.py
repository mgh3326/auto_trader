"""ROB-317 — event → MarketState updater + kline → Candle mapping."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from app.services.brokers.binance.demo_scalping_ws.events import (
    apply_event,
    kline_to_candle,
)
from app.services.brokers.binance.demo_scalping_ws.market_stream import AggTradeEvent
from app.services.brokers.binance.demo_scalping_ws.state import MarketState
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent

_NOW = dt.datetime(2026, 5, 26, 12, 0, 0, tzinfo=dt.UTC)


def _kline(close: str = "0.515") -> KlineEvent:
    return KlineEvent(
        symbol="XRPUSDT",
        interval="1m",
        open_time=dt.datetime(2026, 5, 26, 11, 59, tzinfo=dt.UTC),
        close_time=dt.datetime(2026, 5, 26, 11, 59, 59, tzinfo=dt.UTC),
        open=Decimal("0.50"),
        high=Decimal("0.52"),
        low=Decimal("0.49"),
        close=Decimal(close),
        base_volume=Decimal("1000"),
        quote_volume=Decimal("515"),
        trade_count=42,
        is_closed=True,
    )


def test_apply_book_ticker_updates_quote_and_freshness() -> None:
    state = MarketState(symbol="XRPUSDT")
    ev = BookTickerEvent(
        symbol="XRPUSDT",
        bid_price=Decimal("0.512"),
        bid_qty=Decimal("1"),
        ask_price=Decimal("0.513"),
        ask_qty=Decimal("1"),
        received_at=_NOW,
    )
    apply_event(state, ev)
    assert state.bid_price == Decimal("0.512")
    assert state.ask_price == Decimal("0.513")
    # Freshness comes from the event's own receipt time, not a wall clock.
    assert state.book_ticker_at == _NOW


def test_apply_agg_trade_updates_trade_and_freshness() -> None:
    state = MarketState(symbol="XRPUSDT")
    ev = AggTradeEvent(
        symbol="XRPUSDT",
        price=Decimal("0.5125"),
        qty=Decimal("10"),
        trade_time=_NOW,
        is_buyer_maker=False,
    )
    apply_event(state, ev)
    assert state.last_trade_price == Decimal("0.5125")
    assert state.agg_trade_at == _NOW


def test_apply_kline_does_not_touch_quote_freshness() -> None:
    # A closed kline must NOT mask a dead bookTicker/aggTrade stream.
    state = MarketState(symbol="XRPUSDT")
    apply_event(state, _kline())
    assert state.book_ticker_at is None
    assert state.agg_trade_at is None


def test_kline_to_candle_maps_fields() -> None:
    candle = kline_to_candle(_kline(close="0.515"))
    assert candle.close == Decimal("0.515")
    assert candle.high == Decimal("0.52")
    assert candle.close_time_ms == int(
        dt.datetime(2026, 5, 26, 11, 59, 59, tzinfo=dt.UTC).timestamp() * 1000
    )
