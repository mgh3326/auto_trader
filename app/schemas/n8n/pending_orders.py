# app/schemas/n8n/pending_orders.py
"""Schemas for the n8n pending orders endpoint."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["N8nPendingOrderItem", "N8nPendingOrderSummary", "N8nPendingOrdersResponse"]


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
