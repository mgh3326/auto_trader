from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field


class N8nPendingOrderItem(BaseModel):
    order_id: str = Field(..., description="Unique order identifier")
    symbol: str = Field(
        ..., description="Normalized symbol with any crypto prefix removed"
    )
    name: str | None = Field(
        None,
        description="Human-readable name (e.g. 현대로템 for KR, None for crypto)",
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
    indicators: dict[str, float | None] | None = Field(
        None,
        description="Technical indicators for the order's symbol (RSI, StochRSI, ADX, etc.)",
    )

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "order_id": "1234567890",
                "symbol": "BTC",
                "name": None,
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
                "indicators": {
                    "rsi_14": 58.7,
                    "rsi_7": 62.3,
                    "stoch_rsi_k": 72.5,
                    "stoch_rsi_d": 68.1,
                    "adx": 28.3,
                    "ema_20_distance_pct": 4.2,
                    "change_24h_pct": 3.2,
                    "volume_24h_krw": 285000000000,
                },
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
                        "name": None,
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
                        "indicators": {
                            "rsi_14": 58.7,
                            "rsi_7": 62.3,
                            "stoch_rsi_k": 72.5,
                            "stoch_rsi_d": 68.1,
                            "adx": 28.3,
                            "ema_20_distance_pct": 4.2,
                            "change_24h_pct": 3.2,
                            "volume_24h_krw": 285000000000,
                        },
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


# -----------------------------------------------------------------------------
# Daily Brief (merged from main)
# -----------------------------------------------------------------------------
class N8nDailyBriefPendingMarket(BaseModel):
    """Per-market pending order summary for the daily brief."""

    total: int = Field(0, description="Total pending orders in this market")
    buy_count: int = Field(0, description="Pending buy orders")
    sell_count: int = Field(0, description="Pending sell orders")
    total_buy_fmt: str | None = Field(None, description="Formatted total buy amount")
    total_sell_fmt: str | None = Field(None, description="Formatted total sell amount")
    orders: list[N8nPendingOrderItem] = Field(default_factory=list)


class N8nDailyBriefPendingOrders(BaseModel):
    """Aggregated pending orders across all markets."""

    crypto: N8nDailyBriefPendingMarket | None = Field(None)
    kr: N8nDailyBriefPendingMarket | None = Field(None)
    us: N8nDailyBriefPendingMarket | None = Field(None)


class N8nPortfolioMarketSummary(BaseModel):
    """Per-market portfolio summary."""

    total_value_krw: float | None = Field(None, description="Total value in KRW")
    total_value_usd: float | None = Field(
        None, description="Total value in USD (US only)"
    )
    total_value_fmt: str | None = Field(None, description="Formatted total value")
    pnl_pct: float | None = Field(None, description="Overall P&L percentage")
    pnl_fmt: str | None = Field(None, description="Formatted P&L")
    position_count: int = Field(0, description="Number of positions")
    top_gainers: list[dict[str, object]] = Field(default_factory=list)
    top_losers: list[dict[str, object]] = Field(default_factory=list)


class N8nDailyBriefPortfolio(BaseModel):
    """Portfolio summary across all markets."""

    crypto: N8nPortfolioMarketSummary | None = Field(None)
    kr: N8nPortfolioMarketSummary | None = Field(None)
    us: N8nPortfolioMarketSummary | None = Field(None)


class N8nFillItem(BaseModel):
    """Single filled order for the daily brief."""

    symbol: str = Field(..., description="Symbol")
    market: str = Field(..., description="Market: crypto, kr, us")
    side: str = Field(..., description="buy or sell")
    price_fmt: str = Field(..., description="Formatted fill price")
    amount_fmt: str = Field(..., description="Formatted fill amount")
    time: str = Field(..., description="Fill time HH:MM")


class N8nYesterdayFills(BaseModel):
    """Yesterday's filled orders summary."""

    total: int = Field(0, description="Total fills")
    fills: list[N8nFillItem] = Field(default_factory=list)


class N8nDailyBriefResponse(BaseModel):
    """Daily trading brief response."""

    success: bool = Field(..., description="Whether request completed successfully")
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    date_fmt: str = Field(..., description="Date formatted as MM/DD (요일)")

    market_overview: N8nMarketOverview = Field(..., description="Market-wide context")
    pending_orders: N8nDailyBriefPendingOrders = Field(
        ..., description="Per-market pending orders"
    )
    portfolio_summary: N8nDailyBriefPortfolio = Field(
        ..., description="Per-market portfolio"
    )
    yesterday_fills: N8nYesterdayFills = Field(
        ..., description="Yesterday's filled orders"
    )

    brief_text: str = Field(..., description="Pre-formatted briefing text for Discord")
    errors: list[dict[str, object]] = Field(default_factory=list)

    model_config: ClassVar[ConfigDict] = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "as_of": "2026-03-17T08:30:00+09:00",
                "date_fmt": "03/17 (월)",
                "brief_text": "📋 Daily Trading Brief — 03/17 (월)\n...",
            }
        }
    )


# -----------------------------------------------------------------------------
# Filled Orders
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Trade Reviews (POST + GET stats)
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Pending Review (extended pending-orders)
# -----------------------------------------------------------------------------
class N8nPendingReviewItem(BaseModel):
    """Extends pending orders with fill probability classification."""

    order_id: str = Field(...)
    symbol: str = Field(...)
    name: str | None = Field(None)
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
                "name": None,
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


# -----------------------------------------------------------------------------
# Pending Snapshots (POST + PATCH resolve)
# -----------------------------------------------------------------------------
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


# -----------------------------------------------------------------------------
# Crypto Scan (GET /api/n8n/crypto-scan)
# -----------------------------------------------------------------------------
class N8nCryptoScanParams(BaseModel):
    """Echo of the scan parameters used for this response."""

    top_n: int = Field(..., description="Top N coins by 24h trade amount")
    include_holdings: bool = Field(...)
    include_crash: bool = Field(...)
    include_sma_cross: bool = Field(...)
    include_fear_greed: bool = Field(...)
    ohlcv_days: int = Field(...)


class N8nBtcContext(BaseModel):
    """BTC technical context for market backdrop."""

    rsi14: float | None = Field(None, description="BTC RSI 14-period")
    sma20: float | None = Field(None, description="BTC SMA 20")
    sma60: float | None = Field(None, description="BTC SMA 60")
    sma200: float | None = Field(None, description="BTC SMA 200")
    current_price: float | None = Field(None, description="BTC current price in KRW")
    change_rate_24h: float | None = Field(None, description="BTC 24h change rate")


class N8nCryptoScanIndicators(BaseModel):
    """Per-coin technical indicators."""

    rsi14: float | None = Field(None, description="RSI 14-period")
    sma20: float | None = Field(None, description="SMA 20")
    sma60: float | None = Field(None, description="SMA 60")
    sma200: float | None = Field(None, description="SMA 200")


class N8nSmaCross(BaseModel):
    """SMA20 crossing event data."""

    type: str = Field(..., description="golden or dead")
    prev_close: float = Field(...)
    curr_close: float = Field(...)
    prev_sma20: float = Field(...)
    curr_sma20: float = Field(...)


class N8nCrashData(BaseModel):
    """Crash detection data per coin."""

    change_rate_24h: float = Field(..., description="Actual 24h change rate")
    threshold: float = Field(..., description="Crash threshold for this coin's rank")
    triggered: bool = Field(..., description="Whether abs(change) >= threshold")


class N8nCryptoScanCoin(BaseModel):
    """Single coin in the crypto scan response."""

    symbol: str = Field(..., description="Upbit market code, e.g. KRW-BTC")
    currency: str = Field(..., description="Currency code, e.g. BTC")
    name: str = Field(..., description="Korean name, e.g. 비트코인")
    rank: int | None = Field(
        None, description="Trade amount rank (1-based), null if holding-only"
    )
    is_holding: bool = Field(..., description="Whether user currently holds this coin")
    current_price: float | None = Field(None, description="Current trade price in KRW")
    change_rate_24h: float | None = Field(None, description="24h signed change rate")
    trade_amount_24h: float | None = Field(
        None, description="24h accumulated trade amount in KRW"
    )
    indicators: N8nCryptoScanIndicators = Field(...)
    sma_cross: N8nSmaCross | None = Field(None)
    crash: N8nCrashData | None = Field(None)


class N8nCryptoScanSummary(BaseModel):
    """Aggregate summary of the scan results."""

    total_scanned: int = Field(..., description="Total coins scanned")
    top_n_count: int = Field(..., description="Coins from top N by trade amount")
    holdings_added: int = Field(
        ..., description="Extra coins added because they are held"
    )
    oversold_count: int = Field(0, description="Coins with RSI < 35 (reference only)")
    overbought_count: int = Field(0, description="Coins with RSI > 70 (reference only)")
    crash_triggered_count: int = Field(
        0, description="Coins that triggered crash threshold"
    )
    sma_golden_cross_count: int = Field(0, description="SMA20 golden cross coins")
    sma_dead_cross_count: int = Field(0, description="SMA20 dead cross coins")


class N8nCryptoScanResponse(BaseModel):
    """Top-level response for GET /api/n8n/crypto-scan."""

    success: bool = Field(...)
    as_of: str = Field(..., description="Response timestamp in KST ISO8601")
    scan_params: N8nCryptoScanParams = Field(...)
    btc_context: N8nBtcContext = Field(...)
    fear_greed: N8nFearGreedData | None = Field(None)
    coins: list[N8nCryptoScanCoin] = Field(default_factory=list)
    summary: N8nCryptoScanSummary = Field(...)
    errors: list[dict[str, object]] = Field(default_factory=list)


# -----------------------------------------------------------------------------
# Trade Reviews (GET list)
# -----------------------------------------------------------------------------
class N8nTradeReviewListItem(BaseModel):
    """Single trade review entry for list response."""

    order_id: str = Field(..., description="Broker order ID")
    symbol: str = Field(..., description="Normalized symbol (BTC, 005930, NVDA)")
    market: str = Field(..., description="Market: crypto, kr, us")
    side: str = Field(..., description="buy or sell")
    price: float = Field(..., description="Execution price")
    quantity: float = Field(..., description="Filled quantity")
    total_amount: float = Field(..., description="Total amount (price * quantity)")
    fee: float = Field(0, description="Trading fee")
    currency: str = Field("KRW", description="KRW or USD")
    filled_at: str = Field(..., description="Trade date in ISO8601")
    # review
    verdict: str = Field(..., description="good, neutral, or bad")
    pnl_pct: float | None = Field(None, description="P&L percentage at review")
    comment: str | None = Field(None, description="Review commentary")
    review_type: str = Field("daily", description="daily, weekly, monthly, manual")
    review_date: str = Field(..., description="Review date in ISO8601")
    # snapshot
    indicators: N8nTradeReviewIndicators | None = Field(
        None, description="Technical indicator snapshot at execution time"
    )


class N8nTradeReviewListResponse(BaseModel):
    """Response for GET /api/n8n/trade-reviews."""

    success: bool = Field(...)
    period: str = Field(..., description="Period label, e.g. '2026-03-11 ~ 2026-03-18'")
    total_count: int = Field(..., description="Number of reviews returned")
    reviews: list[N8nTradeReviewListItem] = Field(
        default_factory=list, description="Trade review items"
    )
    errors: list[dict[str, object]] = Field(default_factory=list)


class N8nKrPosition(BaseModel):
    symbol: str
    name: str
    quantity: float = 0
    avg_price: float = 0
    current_price: float | None = None
    eval_krw: float | None = None
    pnl_pct: float | None = None
    pnl_fmt: str | None = None
    eval_fmt: str | None = None
    account: str | None = None


class N8nKrHoldingsAccount(BaseModel):
    total_count: int = 0
    total_eval_krw: float = 0
    total_eval_fmt: str = "0"
    total_pnl_pct: float | None = None
    total_pnl_fmt: str | None = None
    positions: list[N8nKrPosition] = Field(default_factory=list)


class N8nKrHoldings(BaseModel):
    kis: N8nKrHoldingsAccount = Field(default_factory=N8nKrHoldingsAccount)
    toss: N8nKrHoldingsAccount = Field(default_factory=N8nKrHoldingsAccount)
    combined: N8nKrHoldingsAccount = Field(default_factory=N8nKrHoldingsAccount)


class N8nKrCashBalance(BaseModel):
    kis_krw: float = 0
    kis_krw_fmt: str = "0"
    toss_krw: float | None = None
    toss_krw_fmt: str = "수동 관리"
    total_krw: float = 0
    total_krw_fmt: str = "0"


class N8nKrScreenResult(BaseModel):
    symbol: str
    name: str
    current_price: float | None = None
    rsi: float | None = None
    change_pct: float | None = None
    volume_ratio: float | None = None
    market_cap_fmt: str | None = None
    signal: str | None = None
    sector: str | None = None


class N8nKrScreening(BaseModel):
    total_scanned: int = 0
    top_n: int = 0
    strategy: str | None = None
    results: list[N8nKrScreenResult] = Field(default_factory=list)
    summary: dict[str, Any] = Field(default_factory=dict)


class N8nKrMorningReportResponse(BaseModel):
    success: bool
    as_of: str
    date_fmt: str
    holdings: N8nKrHoldings = Field(default_factory=N8nKrHoldings)
    cash_balance: N8nKrCashBalance = Field(default_factory=N8nKrCashBalance)
    screening: N8nKrScreening = Field(default_factory=N8nKrScreening)
    pending_orders: dict[str, Any] = Field(default_factory=dict)
    brief_text: str = ""
    errors: list[dict[str, str]] = Field(default_factory=list)
