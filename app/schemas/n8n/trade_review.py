# app/schemas/n8n/trade_review.py
"""Schemas for the n8n trade review endpoints."""

from __future__ import annotations

from pydantic import BaseModel, Field

__all__ = [
    "N8nTradeReviewIndicators",
    "N8nTradeReviewItem",
    "N8nTradeReviewsRequest",
    "N8nTradeReviewsResponse",
    "N8nRsiZoneStats",
    "N8nTradeReviewStats",
    "N8nTradeReviewStatsResponse",
    "N8nTradeReviewListItem",
    "N8nTradeReviewListResponse",
]


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
