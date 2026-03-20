from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass(slots=True)
class Quote:
    symbol: str
    market: str
    price: float
    source: str
    previous_close: float | None = None
    open: float | None = None
    high: float | None = None
    low: float | None = None
    volume: int | None = None
    value: float | None = None


@dataclass(slots=True)
class Candle:
    symbol: str
    market: str
    source: str
    period: str
    timestamp: dt.datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    value: float | None = None


@dataclass(slots=True)
class OrderbookLevel:
    price: float
    quantity: float


@dataclass(slots=True)
class OrderbookSnapshot:
    symbol: str
    instrument_type: str
    source: str
    asks: list[OrderbookLevel]
    bids: list[OrderbookLevel]
    total_ask_qty: float
    total_bid_qty: float
    bid_ask_ratio: float | None
    expected_price: int | None = None
    expected_qty: int | None = None


__all__ = ["Quote", "Candle", "OrderbookLevel", "OrderbookSnapshot"]
