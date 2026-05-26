"""ROB-317 — event → state mutation + kline → Candle mapping (pure).

``apply_event`` updates quote/trade fields + freshness timestamps from
bookTicker/aggTrade events. Freshness is stamped from each event's OWN
receipt/trade time (design §5: "last event received"), not a wall clock, so
the supervisor's trigger-time clock measures true staleness against it.
Closed klines deliberately do NOT update quote freshness (a dead bookTicker
stream must still trip STALE_DATA); klines are routed to the signal buffer
by the supervisor instead.
"""

from __future__ import annotations

from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.brokers.binance.demo_scalping_ws.market_stream import (
    AggTradeEvent,
    FuturesWsEvent,
)
from app.services.brokers.binance.demo_scalping_ws.state import MarketState
from app.services.brokers.binance.ws_client import BookTickerEvent, KlineEvent


def apply_event(state: MarketState, event: FuturesWsEvent) -> None:
    """Mutate ``state`` from a market event. Klines are a no-op here.

    Freshness timestamps come from the event itself (``received_at`` for
    bookTicker, ``trade_time`` for aggTrade).
    """
    if isinstance(event, BookTickerEvent):
        state.bid_price = event.bid_price
        state.ask_price = event.ask_price
        state.book_ticker_at = event.received_at
    elif isinstance(event, AggTradeEvent):
        state.last_trade_price = event.price
        state.agg_trade_at = event.trade_time
    # KlineEvent: intentionally no state mutation (routed to the signal buffer).


def kline_to_candle(event: KlineEvent) -> Candle:
    """Map a closed-kline event to the signal's Candle value object."""
    return Candle(
        open_time_ms=int(event.open_time.timestamp() * 1000),
        open=event.open,
        high=event.high,
        low=event.low,
        close=event.close,
        close_time_ms=int(event.close_time.timestamp() * 1000),
    )
