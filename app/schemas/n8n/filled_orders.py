# app/schemas/n8n/filled_orders.py
"""Schemas for the n8n filled orders endpoint."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["N8nFilledOrderItem", "N8nFilledOrdersResponse"]


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
