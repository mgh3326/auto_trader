"""ROB-285 — Normalized DTOs for the Binance public adapter.

SDK / wire types are never returned across the adapter boundary. All
caller-visible data structures are defined here.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class BinanceExchangeSymbolInfo:
    symbol: str
    base_asset: str
    quote_asset: str
    status: str  # TRADING / BREAK / HALT / etc.


@dataclass(frozen=True, slots=True)
class BinanceKlineRow:
    symbol: str
    interval: str
    open_time: dt.datetime
    close_time: dt.datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    base_volume: Decimal
    quote_volume: Decimal | None
    trade_count: int | None
    taker_buy_base_volume: Decimal | None
    taker_buy_quote_volume: Decimal | None
    is_closed: bool

    @property
    def event_at(self) -> dt.datetime:
        # For REST klines, the row's source_event_at is close_time.
        return self.close_time


@dataclass(frozen=True, slots=True)
class BinanceBookTicker:
    symbol: str
    bid_price: Decimal
    bid_qty: Decimal
    ask_price: Decimal
    ask_qty: Decimal
    fetched_at: dt.datetime
