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
    fill_proximity: str | None = Field(
        None, description="Fill proximity classification: near, moderate, far, very_far"
    )
    fill_proximity_fmt: str | None = Field(
        None, description="Formatted fill proximity, e.g. '체결 임박 ⚡'"
    )
    needs_attention: bool = Field(
        False, description="Whether this order needs user attention"
    )
    attention_reason: str | None = Field(
        None, description="Human-readable reason for attention"
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
                "fill_proximity": "near",
                "fill_proximity_fmt": "체결 임박 ⚡",
                "needs_attention": True,
                "attention_reason": "체결 임박 (+0.5%)",
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
    near_fill_count: int = Field(
        0, description="Number of orders near fill (within near_fill_pct)"
    )
    needs_attention_count: int = Field(
        0, description="Number of orders needing attention"
    )
    attention_orders_only: list[N8nPendingOrderItem] = Field(
        default_factory=list,
        description="Orders that need attention (populated when attention_only=true)",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "total": 2,
                "buy_count": 1,
                "sell_count": 1,
                "total_buy_krw": 297000.0,
                "total_sell_krw": 1825000.0,
                "near_fill_count": 1,
                "needs_attention_count": 1,
                "attention_orders_only": [],
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
                        "fill_proximity": "near",
                        "fill_proximity_fmt": "체결 임박 ⚡",
                        "needs_attention": True,
                        "attention_reason": "체결 임박 (+0.5%)",
                    }
                ],
                "summary": {
                    "total": 1,
                    "buy_count": 1,
                    "sell_count": 0,
                    "total_buy_krw": 297000.0,
                    "total_sell_krw": 0.0,
                    "near_fill_count": 1,
                    "needs_attention_count": 1,
                    "attention_orders_only": [],
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


# ---------------------------------------------------------------------------
# Filled Orders
# ---------------------------------------------------------------------------
class N8nFilledOrderItem(BaseModel):
    symbol: str = Field(..., description="Normalized symbol (e.g. BTC, 005930, NVDA)")
    raw_symbol: str = Field(..., description="Original broker symbol (e.g. KRW-BTC)")
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(..., description="buy or sell")
    price: float = Field(..., description="Execution price")
    quantity: float = Field(..., description="Filled quantity")
    total_amount: float = Field(
        ..., description="Total filled amount (price * quantity)"
    )
    fee: float = Field(0, description="Trading fee")
    currency: str = Field(..., description="KRW or USD")
    account: str = Field(
        ..., description="Account identifier: upbit, kis, kis_overseas"
    )
    order_id: str = Field(..., description="Unique order identifier from broker")
    filled_at: str = Field(..., description="Execution timestamp in KST ISO8601")
    current_price: float | None = Field(None, description="Current market price")
    pnl_pct: float | None = Field(None, description="Unrealized P&L percentage")
    pnl_pct_fmt: str | None = Field(None, description="Formatted P&L, e.g. +3.27%")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "instrument_type": "crypto",
                "side": "buy",
                "price": 98000000,
                "quantity": 0.015,
                "total_amount": 1470000,
                "fee": 735,
                "currency": "KRW",
                "account": "upbit",
                "order_id": "abc-123-def",
                "filled_at": "2026-03-17T14:30:00+09:00",
                "current_price": 101200000,
                "pnl_pct": 3.27,
                "pnl_pct_fmt": "+3.27%",
            }
        }
    )


class N8nFilledOrdersResponse(BaseModel):
    success: bool = Field(..., description="Whether the request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    total_count: int = Field(..., description="Total number of filled orders returned")
    orders: list[N8nFilledOrderItem] = Field(
        default_factory=list, description="Filled order items"
    )
    errors: list[dict[str, object]] = Field(
        default_factory=list,
        description="Non-fatal errors from individual market fetches",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-17T20:00:00+09:00",
                "total_count": 0,
                "orders": [],
                "errors": [],
            }
        }
    )


# ---------------------------------------------------------------------------
# Trade Reviews (POST + GET stats)
# ---------------------------------------------------------------------------
class N8nTradeReviewIndicators(BaseModel):
    rsi_14: float | None = Field(None, description="RSI 14-period")
    rsi_7: float | None = Field(None, description="RSI 7-period")
    ema_20: float | None = Field(None, description="EMA 20")
    ema_200: float | None = Field(None, description="EMA 200")
    macd: float | None = Field(None, description="MACD value")
    macd_signal: float | None = Field(None, description="MACD signal line")
    adx: float | None = Field(None, description="ADX value")
    stoch_rsi_k: float | None = Field(None, description="Stochastic RSI K")
    volume_ratio: float | None = Field(None, description="Volume ratio vs 20d avg")
    fear_greed: int | None = Field(None, description="Fear & Greed Index 0-100")


class N8nTradeReviewItem(BaseModel):
    order_id: str = Field(..., description="Broker order ID (required, non-null)")
    account: str = Field(..., description="Account: upbit, kis, kis_overseas")
    symbol: str = Field(..., description="Normalized symbol")
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(..., description="buy or sell")
    price: float = Field(..., description="Execution price")
    quantity: float = Field(..., description="Filled quantity")
    total_amount: float = Field(..., description="Total amount")
    fee: float = Field(0, description="Trading fee")
    currency: str = Field("KRW", description="KRW or USD")
    filled_at: str = Field(..., description="Execution timestamp ISO8601")
    price_at_review: float | None = Field(
        None, description="Current price at review time"
    )
    pnl_pct: float | None = Field(None, description="P&L percentage")
    verdict: str = Field(..., description="good, neutral, or bad")
    comment: str | None = Field(None, description="Review commentary")
    review_type: str = Field("daily", description="daily, weekly, monthly, manual")
    indicators: N8nTradeReviewIndicators | None = Field(
        None, description="Technical indicator snapshot at execution time"
    )


class N8nTradeReviewsRequest(BaseModel):
    reviews: list[N8nTradeReviewItem] = Field(
        ..., description="List of trade reviews to save", min_length=1
    )


class N8nTradeReviewsResponse(BaseModel):
    success: bool = Field(...)
    saved_count: int = Field(..., description="Number of reviews saved")
    skipped_count: int = Field(
        0, description="Number skipped (duplicate trade or existing review)"
    )
    errors: list[dict[str, object]] = Field(default_factory=list)


class N8nRsiZoneStats(BaseModel):
    count: int = Field(...)
    avg_pnl: float | None = Field(None)
    win_rate: float | None = Field(None)


class N8nTradeReviewStats(BaseModel):
    period: str = Field(..., description="Period label, e.g. 2026-03-10 ~ 2026-03-17")
    total_trades: int = Field(0)
    buy_count: int = Field(0)
    sell_count: int = Field(0)
    win_rate: float | None = Field(
        None, description="Percentage of trades with pnl > 0"
    )
    avg_pnl_pct: float | None = Field(None)
    best_trade: dict[str, object] | None = Field(None)
    worst_trade: dict[str, object] | None = Field(None)
    by_verdict: dict[str, int] = Field(default_factory=dict)
    by_rsi_zone: dict[str, N8nRsiZoneStats] = Field(default_factory=dict)


class N8nTradeReviewStatsResponse(BaseModel):
    success: bool = Field(...)
    stats: N8nTradeReviewStats = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pending Review (extended pending-orders)
# ---------------------------------------------------------------------------
class N8nPendingReviewItem(BaseModel):
    """Extends pending orders with fill probability classification."""

    order_id: str = Field(...)
    symbol: str = Field(...)
    raw_symbol: str = Field(...)
    market: str = Field(...)
    side: str = Field(...)
    order_price: float = Field(...)
    current_price: float | None = Field(None)
    gap_pct: float | None = Field(None)
    gap_pct_fmt: str | None = Field(None)
    amount_krw: float | None = Field(None)
    quantity: float = Field(...)
    remaining_qty: float = Field(...)
    created_at: str = Field(...)
    age_days: int = Field(...)
    currency: str = Field(...)
    days_pending: int = Field(..., description="Days since order creation")
    fill_probability: str = Field(..., description="high, medium, low, or stale")
    suggestion: str | None = Field(None, description="Action suggestion in Korean")

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "order_id": "xyz-456",
                "symbol": "BTC",
                "raw_symbol": "KRW-BTC",
                "market": "crypto",
                "side": "buy",
                "order_price": 96500000,
                "current_price": 101200000,
                "gap_pct": -4.6,
                "gap_pct_fmt": "-4.6%",
                "amount_krw": 965000,
                "quantity": 0.01,
                "remaining_qty": 0.01,
                "created_at": "2026-03-14T10:00:00+09:00",
                "age_days": 3,
                "currency": "KRW",
                "days_pending": 3,
                "fill_probability": "medium",
                "suggestion": "가격 조정 검토",
            }
        }
    )


class N8nPendingReviewResponse(BaseModel):
    success: bool = Field(...)
    as_of: str = Field(...)
    total_count: int = Field(...)
    orders: list[N8nPendingReviewItem] = Field(default_factory=list)
    errors: list[dict[str, object]] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pending Snapshots (POST + PATCH resolve)
# ---------------------------------------------------------------------------
class N8nPendingSnapshotItem(BaseModel):
    symbol: str = Field(...)
    instrument_type: str = Field(..., description="crypto, equity_kr, equity_us")
    side: str = Field(...)
    order_price: float = Field(...)
    quantity: float = Field(...)
    current_price: float | None = Field(None)
    gap_pct: float | None = Field(None)
    days_pending: int | None = Field(None)
    account: str = Field(...)
    order_id: str | None = Field(None)


class N8nPendingSnapshotsRequest(BaseModel):
    snapshots: list[N8nPendingSnapshotItem] = Field(
        ..., min_length=1, description="Pending order snapshots to save"
    )


class N8nPendingSnapshotsResponse(BaseModel):
    success: bool = Field(...)
    saved_count: int = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)


class N8nPendingResolutionItem(BaseModel):
    order_id: str = Field(...)
    account: str = Field(...)
    resolved_as: str = Field(..., description="filled, cancelled, or expired")


class N8nPendingResolveRequest(BaseModel):
    resolutions: list[N8nPendingResolutionItem] = Field(
        ..., min_length=1, description="Resolutions to apply"
    )


class N8nPendingResolveResponse(BaseModel):
    success: bool = Field(...)
    resolved_count: int = Field(...)
    not_found_count: int = Field(0)
    errors: list[dict[str, object]] = Field(default_factory=list)
