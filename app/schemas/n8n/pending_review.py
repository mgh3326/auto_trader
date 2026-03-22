# app/schemas/n8n/pending_review.py
"""Schemas for the n8n pending review endpoint."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["N8nPendingReviewItem", "N8nPendingReviewResponse"]


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
    action_context: dict[str, dict[str, object]] | None = Field(
        None, description="Action metadata for Discord buttons"
    )

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
