from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class PositionDetailComponentResponse(BaseModel):
    broker: str
    account_name: str
    source: str
    quantity: float
    avg_price: float
    current_price: float | None = None
    evaluation: float | None = None
    profit_loss: float | None = None
    profit_rate: float | None = None


class PositionDetailSummaryResponse(BaseModel):
    market_type: str
    symbol: str
    name: str
    current_price: float | None = None
    quantity: float
    avg_price: float
    profit_loss: float | None = None
    profit_rate: float | None = None
    evaluation: float | None = None
    account_count: int
    target_distance_pct: float | None = None
    stop_distance_pct: float | None = None


class PositionDetailPageResponse(BaseModel):
    summary: PositionDetailSummaryResponse
    components: list[PositionDetailComponentResponse]
    journal: dict[str, Any] | None = None
