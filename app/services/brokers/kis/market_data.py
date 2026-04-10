"""KIS market data client — facade combining domestic and overseas operations."""

# pyright: reportImplicitStringConcatenation=false, reportMissingTypeArgument=false
from __future__ import annotations

from ._base_market_data import _aggregate_minute_candles_frame
from .domestic_market_data import (
    DomesticMarketDataMixin,
    normalize_daily_chart_lookback,
)
from .overseas_market_data import OverseasMarketDataMixin, OverseasMinuteChartPage

__all__ = [
    "MarketDataClient",
    "OverseasMinuteChartPage",
    "_aggregate_minute_candles_frame",
    "normalize_daily_chart_lookback",
]


class MarketDataClient(DomesticMarketDataMixin, OverseasMarketDataMixin):
    """Client for KIS market data operations.

    Handles price data, charts, orderbook, and ranking information.
    Combines domestic and overseas market data functionality via mixin inheritance.
    """
