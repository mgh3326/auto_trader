# app/schemas/n8n/market_context.py
"""Schemas for the n8n market context endpoint."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.n8n.common import N8nMarketOverview

__all__ = ["N8nSymbolContext", "N8nMarketContextSummary", "N8nMarketContextResponse"]


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
