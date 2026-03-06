"""Type stubs for tvscreener library."""

from enum import Enum
from typing import TypeVar

import pandas as pd

# Generic types
T = TypeVar("T")

class CryptoScreener:
    """CryptoScreener for cryptocurrency screening."""

    def __init__(self) -> None: ...
    def select(self, *fields: CryptoField) -> CryptoScreener: ...
    def where(self, clause: str) -> CryptoScreener: ...
    def get(self) -> pd.DataFrame: ...

class StockScreener:
    """StockScreener for stock screening."""

    def __init__(self) -> None: ...
    def select(self, *fields: StockField) -> StockScreener: ...
    def where(self, clause: str) -> StockScreener: ...
    def get(self) -> pd.DataFrame: ...

class CryptoField(Enum):
    """Enum for CryptoScreener fields."""

    PRICE = "price"
    VOLUME = "volume"
    RELATIVE_STRENGTH_INDEX_14 = "relative_strength_index_14"
    AVERAGE_DIRECTIONAL_INDEX_14 = "average_directional_index_14"
    MARKET_CAP = "market_cap"
    MARKET_CAP_RANK = "market_cap_rank"
    CHANGE_PERCENT = "change_percent"
    MACD = "macd"
    SMA_50 = "sma_50"
    SMA_200 = "sma_200"
    ATR_14 = "atr_14"

class StockField(Enum):
    """Enum for StockScreener fields."""

    PRICE = "price"
    VOLUME = "volume"
    RELATIVE_STRENGTH_INDEX_14 = "relative_strength_index_14"
    AVERAGE_DIRECTIONAL_INDEX_14 = "average_directional_index_14"
    MARKET_CAP = "market_cap"
    MARKET_CAP_RANK = "market_cap_rank"
    CHANGE_PERCENT = "change_percent"
    MACD = "macd"
    SMA_50 = "sma_50"
    SMA_200 = "sma_200"
    ATR_14 = "atr_14"
    COUNTRY = "country"
    PRICE_EARNINGS_RATIO = "price_earnings_ratio"
    PRICE_BOOK_RATIO = "price_book_ratio"
    DIVIDEND_YIELD = "dividend_yield"

class MalformedRequestException(Exception):
    """Exception for malformed requests to TradingView API."""

    pass

# Module-level exports
__all__ = [
    "CryptoScreener",
    "StockScreener",
    "CryptoField",
    "StockField",
    "MalformedRequestException",
]
