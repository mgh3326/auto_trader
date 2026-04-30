# app/schemas/n8n/daily_brief.py
"""Schemas for the n8n daily brief endpoint."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.n8n.common import N8nMarketOverview
from app.schemas.n8n.pending_orders import N8nPendingOrderItem

__all__ = [
    "N8nDailyBriefPendingMarket",
    "N8nDailyBriefPendingOrders",
    "N8nPortfolioMarketSummary",
    "N8nDailyBriefPortfolio",
    "N8nFillItem",
    "N8nYesterdayFills",
    "N8nDailyBurnStatus",
    "N8nDailyBriefResponse",
]


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


class N8nDailyBurnStatus(BaseModel):
    """Recomputed active DCA daily-burn summary."""

    daily_burn_krw: float = Field(0, description="Recomputed daily burn in KRW")
    active_count: int = Field(0, description="Active DCA journal count")
    per_record: list[dict[str, object]] = Field(default_factory=list)
    days_to_next_obligation: int | None = Field(
        None, description="Days until the next active DCA obligation"
    )
    cash_needed_until_obligation: float = Field(
        0, description="Projected cash needed until next obligation"
    )
    error: str | None = Field(
        None, description="Failure detail when daily-burn recomputation degraded"
    )


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
    daily_burn: N8nDailyBurnStatus | None = Field(
        None, description="Recomputed active DCA daily-burn status"
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
