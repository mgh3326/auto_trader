from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class N8nPendingOrderItem(BaseModel):
    order_id: str = Field(..., description="Unique order identifier")
    symbol: str = Field(
        ..., description="Normalized symbol with any crypto prefix removed"
    )
    raw_symbol: str = Field(..., description="Original symbol returned by the broker")
    market: str = Field(..., description="Market code: crypto, kr, or us")
    side: str = Field(..., description="Order side: buy or sell")
    status: str = Field(..., description="Order status: pending or partial")
    order_price: float = Field(..., description="Order price")
    current_price: float | None = Field(None, description="Current market price")
    gap_pct: float | None = Field(
        None,
        description="Gap between order price and current price in percent",
    )
    amount_krw: float | None = Field(
        None,
        description="Estimated order amount in KRW; null when USD/KRW conversion is unavailable",
    )
    quantity: float = Field(..., description="Originally ordered quantity")
    remaining_qty: float = Field(..., description="Remaining unfilled quantity")
    created_at: str = Field(..., description="Order creation time in KST ISO8601")
    age_hours: int = Field(..., description="Hours since order creation, floored")
    age_days: int = Field(
        ..., description="Days since order creation, computed from hours"
    )
    currency: str = Field(..., description="Order currency: KRW or USD")
    # Pre-formatted display fields (populated by server, None if not enriched)
    order_price_fmt: str | None = Field(
        None, description="Formatted order price for display"
    )
    current_price_fmt: str | None = Field(
        None, description="Formatted current price for display"
    )
    gap_pct_fmt: str | None = Field(
        None, description="Formatted gap percentage with sign, e.g. +14.0%"
    )
    amount_fmt: str | None = Field(
        None, description="Formatted KRW amount, e.g. 31.2만"
    )
    age_fmt: str | None = Field(None, description="Formatted age, e.g. 1일 or 5시간")
    summary_line: str | None = Field(
        None,
        description="One-line order summary, e.g. APT buy @2,470 (현재 2,166, +14.0%, 31.2만, 1일)",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "order_id": "1234567890",
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "market": "crypto",
                "side": "buy",
                "status": "pending",
                "order_price": 148500000.0,
                "current_price": 149200000.0,
                "gap_pct": 0.47,
                "amount_krw": 297000.0,
                "quantity": 0.002,
                "remaining_qty": 0.002,
                "created_at": "2026-03-15T10:30:00+09:00",
                "age_hours": 6,
                "age_days": 0,
                "currency": "KRW",
            }
        }
    )


class N8nPendingOrderSummary(BaseModel):
    total: int = Field(..., description="Total number of pending orders")
    buy_count: int = Field(..., description="Number of pending buy orders")
    sell_count: int = Field(..., description="Number of pending sell orders")
    total_buy_krw: float = Field(
        ...,
        description="Total pending buy amount in KRW for orders with available KRW amounts",
    )
    total_sell_krw: float = Field(
        ...,
        description="Total pending sell amount in KRW for orders with available KRW amounts",
    )
    # Pre-formatted display fields
    total_buy_fmt: str | None = Field(
        None, description="Formatted total buy amount, e.g. 47.8만"
    )
    total_sell_fmt: str | None = Field(
        None, description="Formatted total sell amount, e.g. 3,460.4만"
    )
    title: str | None = Field(
        None,
        description="Summary title line, e.g. 📋 미체결 리뷰 — 03/16 (13건, 매수 4 / 매도 9)",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 2,
                "buy_count": 1,
                "sell_count": 1,
                "total_buy_krw": 297000.0,
                "total_sell_krw": 1825000.0,
            }
        }
    )


class N8nPendingOrdersResponse(BaseModel):
    success: bool = Field(..., description="Whether the request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    market: str = Field(..., description="Market filter applied to the response")
    orders: list[N8nPendingOrderItem] = Field(
        ..., description="Pending order items returned for the market"
    )
    summary: N8nPendingOrderSummary = Field(
        ..., description="Summary totals for the returned pending orders"
    )
    errors: list[dict[str, object]] = Field(
        default_factory=list,
        description="Non-fatal errors collected while building the response, including partial enrichment failures",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-15T16:45:00+09:00",
                "market": "crypto",
                "orders": [
                    {
                        "order_id": "1234567890",
                        "symbol": "BTC",
                        "raw_symbol": "KRW-BTC",
                        "market": "crypto",
                        "side": "buy",
                        "status": "pending",
                        "order_price": 148500000.0,
                        "current_price": 149200000.0,
                        "gap_pct": 0.47,
                        "amount_krw": 297000.0,
                        "quantity": 0.002,
                        "remaining_qty": 0.002,
                        "created_at": "2026-03-15T10:30:00+09:00",
                        "age_hours": 6,
                        "age_days": 0,
                        "currency": "KRW",
                    }
                ],
                "summary": {
                    "total": 1,
                    "buy_count": 1,
                    "sell_count": 0,
                    "total_buy_krw": 297000.0,
                    "total_sell_krw": 0.0,
                },
                "errors": [],
            }
        }
    )


class N8nFearGreedData(BaseModel):
    """Fear & Greed Index data from alternative.me"""

    value: int = Field(..., description="Fear & Greed index value 0-100")
    label: str = Field(
        ..., description="Label: Extreme Fear / Fear / Neutral / Greed / Extreme Greed"
    )
    previous: int = Field(..., description="Previous day's value")
    trend: str = Field(..., description="Trend: improving / stable / deteriorating")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "value": 34,
                "label": "Fear",
                "previous": 28,
                "trend": "improving",
            }
        }
    )


class N8nEconomicEvent(BaseModel):
    """Single economic calendar event"""

    time: str = Field(..., description="Event time in KST (e.g. '21:30 KST')")
    event: str = Field(..., description="Event name (e.g. 'US CPI')")
    importance: str = Field(..., description="Importance: high / medium / low")
    previous: str | None = Field(None, description="Previous value")
    forecast: str | None = Field(None, description="Forecast value")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "time": "21:30 KST",
                "event": "US CPI",
                "importance": "high",
                "previous": "2.4%",
                "forecast": "2.3%",
            }
        }
    )


class N8nMarketOverview(BaseModel):
    """Overall market context and sentiment"""

    fear_greed: N8nFearGreedData | None = Field(None, description="Fear & Greed Index")
    btc_dominance: float | None = Field(None, description="BTC market cap dominance %")
    total_market_cap_change_24h: float | None = Field(
        None, description="Total crypto market cap 24h change %"
    )
    economic_events_today: list[N8nEconomicEvent] = Field(
        default_factory=list, description="Today's economic events"
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "fear_greed": {
                    "value": 34,
                    "label": "Fear",
                    "previous": 28,
                    "trend": "improving",
                },
                "btc_dominance": 61.2,
                "total_market_cap_change_24h": 2.3,
                "economic_events_today": [],
            }
        }
    )


class N8nSymbolContext(BaseModel):
    """Per-symbol market context with indicators"""

    symbol: str = Field(..., description="Normalized symbol (e.g. 'BTC')")
    raw_symbol: str = Field(..., description="Original broker symbol (e.g. 'KRW-BTC')")
    current_price: float = Field(..., description="Current price")
    current_price_fmt: str | None = Field(
        None, description="Formatted price for display"
    )
    change_24h_pct: float | None = Field(None, description="24h price change %")
    change_24h_fmt: str | None = Field(None, description="Formatted 24h change")
    volume_24h_krw: float | None = Field(None, description="24h traded value in KRW")
    volume_24h_fmt: str | None = Field(None, description="Formatted 24h volume")
    rsi_14: float | None = Field(None, description="RSI 14 period")
    rsi_7: float | None = Field(None, description="RSI 7 period (short-term)")
    stoch_rsi_k: float | None = Field(None, description="Stochastic RSI K value")
    adx: float | None = Field(None, description="Average Directional Index")
    ema_20_distance_pct: float | None = Field(
        None, description="Distance from EMA 20 in %"
    )
    trend: str = Field(..., description="Trend: bullish / bearish / neutral")
    trend_strength: str = Field(..., description="Strength: strong / moderate / weak")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "current_price": 108600000,
                "current_price_fmt": "1.09억",
                "change_24h_pct": 3.2,
                "change_24h_fmt": "+3.2%",
                "volume_24h_krw": 285000000000,
                "volume_24h_fmt": "2,850억",
                "rsi_14": 61.1,
                "rsi_7": 65.3,
                "stoch_rsi_k": 72.5,
                "adx": 28.3,
                "ema_20_distance_pct": 4.2,
                "trend": "bullish",
                "trend_strength": "moderate",
            }
        }
    )


class N8nMarketContextSummary(BaseModel):
    """Summary statistics for all symbols"""

    total_symbols: int = Field(..., description="Total number of symbols analyzed")
    bullish_count: int = Field(..., description="Number of bullish symbols")
    bearish_count: int = Field(..., description="Number of bearish symbols")
    neutral_count: int = Field(..., description="Number of neutral symbols")
    avg_rsi: float | None = Field(None, description="Average RSI across symbols")
    market_sentiment: str = Field(
        ...,
        description="Overall sentiment: cautiously_bullish / cautiously_bearish / neutral",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "total_symbols": 8,
                "bullish_count": 5,
                "bearish_count": 1,
                "neutral_count": 2,
                "avg_rsi": 57.3,
                "market_sentiment": "cautiously_bullish",
            }
        }
    )


class N8nMarketContextResponse(BaseModel):
    """Market context API response"""

    success: bool = Field(..., description="Whether request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    market: str = Field(..., description="Market type: crypto, kr, us, all")
    market_overview: N8nMarketOverview = Field(..., description="Market-wide context")
    symbols: list[N8nSymbolContext] = Field(
        default_factory=list, description="Per-symbol analysis"
    )
    summary: N8nMarketContextSummary = Field(..., description="Aggregate statistics")
    errors: list[dict[str, object]] = Field(
        default_factory=list,
        description="Non-fatal errors from individual symbol fetches or external APIs",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-16T09:00:00+09:00",
                "market": "crypto",
                "market_overview": {
                    "fear_greed": {
                        "value": 34,
                        "label": "Fear",
                        "previous": 28,
                        "trend": "improving",
                    },
                    "btc_dominance": 61.2,
                    "total_market_cap_change_24h": 2.3,
                    "economic_events_today": [],
                },
                "symbols": [],
                "summary": {
                    "total_symbols": 0,
                    "bullish_count": 0,
                    "bearish_count": 0,
                    "neutral_count": 0,
                    "avg_rsi": None,
                    "market_sentiment": "neutral",
                },
                "errors": [],
            }
        }
    )
