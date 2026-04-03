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


class PositionDetailWeightsResponse(BaseModel):
    portfolio_weight_pct: float | None = None
    market_weight_pct: float | None = None


class PositionDetailActionSummaryResponse(BaseModel):
    status: str
    status_tone: str = "neutral"
    tags: list[str] = Field(default_factory=list)
    reason: str | None = None
    short_reason: str | None = None


class PositionDetailPageResponse(BaseModel):
    summary: PositionDetailSummaryResponse
    components: list[PositionDetailComponentResponse]
    journal: dict[str, Any] | None = None
    weights: PositionDetailWeightsResponse = Field(
        default_factory=PositionDetailWeightsResponse
    )
    action_summary: PositionDetailActionSummaryResponse | None = None


class PositionIndicatorsResponse(BaseModel):
    price: float | None = None
    indicators: dict[str, Any] = Field(default_factory=dict)
    summary_cards: list[dict[str, str]] = Field(default_factory=list)


class PositionNewsItemResponse(BaseModel):
    title: str
    source: str | None = None
    published_at: str | None = None
    url: str | None = None
    summary: str | None = None
    excerpt: str | None = None
    sentiment: str | None = None
    relevance: str | None = None


class PositionNewsResponse(BaseModel):
    count: int = 0
    news: list[PositionNewsItemResponse] = Field(default_factory=list)


class PositionOpinionSummaryCard(BaseModel):
    label: str
    value: str
    tone: str = "neutral"


class PositionOpinionItemResponse(BaseModel):
    firm: str | None = None
    rating: str | None = None
    target_price: float | None = None
    date: str | None = None


class PositionOpinionsResponse(BaseModel):
    supported: bool = True
    message: str | None = None
    consensus: dict[str, Any] | str | None = None
    avg_target_price: float | None = None
    upside_pct: float | None = None
    buy_count: int | None = None
    hold_count: int | None = None
    sell_count: int | None = None
    summary_cards: list[PositionOpinionSummaryCard] = Field(default_factory=list)
    distribution: dict[str, int] = Field(default_factory=dict)
    top_opinions: list[PositionOpinionItemResponse] = Field(default_factory=list)
    overflow_count: int = 0
    opinions: list[dict[str, Any]] = Field(default_factory=list)


class PositionOrderItemResponse(BaseModel):
    order_id: str
    side: str
    side_label: str | None = None
    status: str
    status_label: str | None = None
    status_tone: str | None = None
    ordered_at: str | None = None
    filled_at: str | None = None
    price: float | None = None
    quantity: float | None = None
    remaining_quantity: float | None = None
    amount: float | None = None
    currency: str | None = None


class PositionOrdersSummaryResponse(BaseModel):
    last_fill: dict[str, Any] | None = None
    last_fill_summary: str | None = None
    pending_count: int = 0
    fill_count: int = 0


class PositionOrdersResponse(BaseModel):
    summary: PositionOrdersSummaryResponse
    recent_fills: list[PositionOrderItemResponse] = Field(default_factory=list)
    pending_orders: list[PositionOrderItemResponse] = Field(default_factory=list)
    errors: list[dict[str, str]] = Field(default_factory=list)
