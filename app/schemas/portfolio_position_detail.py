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


class PositionIndicatorsResponse(BaseModel):
    price: float | None = None
    indicators: dict[str, Any] = Field(default_factory=dict)
    summary_cards: list[dict[str, str]] = Field(default_factory=list)


class PositionNewsResponse(BaseModel):
    count: int = 0
    news: list[dict[str, Any]] = Field(default_factory=list)


class PositionOpinionsResponse(BaseModel):
    supported: bool = True
    message: str | None = None
    consensus: dict[str, Any] | str | None = None
    avg_target_price: float | None = None
    upside_pct: float | None = None
    buy_count: int | None = None
    hold_count: int | None = None
    sell_count: int | None = None
    opinions: list[dict[str, Any]] = Field(default_factory=list)
