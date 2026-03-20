# app/schemas/n8n/common.py
"""Shared schemas used across multiple n8n service modules."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["N8nFearGreedData", "N8nEconomicEvent", "N8nMarketOverview"]


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
