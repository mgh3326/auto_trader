"""ROB-112 — Pydantic schemas for the research pipeline."""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, ConfigDict


class StageVerdict(str, Enum):
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"
    UNAVAILABLE = "unavailable"


class SummaryDecision(str, Enum):
    BUY = "buy"
    HOLD = "hold"
    SELL = "sell"


class SourceFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    newest_age_minutes: int = Field(ge=0)
    oldest_age_minutes: int = Field(ge=0)
    missing_sources: list[str] = Field(default_factory=list)
    stale_flags: list[str] = Field(default_factory=list)
    source_count: int = Field(ge=0)


class MarketSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    last_close: float
    change_pct: float
    rsi_14: float = Field(ge=0, le=100)
    atr_14: float = Field(ge=0)
    volume_ratio_20d: float = Field(ge=0)
    trend: Literal["uptrend", "downtrend", "flat", "unknown"]


class NewsSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    headline_count: int = Field(ge=0)
    sentiment_score: float = Field(ge=-1.0, le=1.0)
    top_themes: list[str] = Field(default_factory=list, max_length=10)
    urgent_flags: list[str] = Field(default_factory=list)


class FundamentalsSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    per: float | None = None
    pbr: float | None = None
    market_cap: float | None = Field(default=None, ge=0)
    sector: str | None = None
    peer_count: int = Field(default=0, ge=0)
    relative_per_vs_peers: float | None = None


class SocialSignals(BaseModel):
    model_config = ConfigDict(extra="forbid")

    available: bool
    reason: str
    phase: str = "placeholder"


class BullBearArgument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    cited_stage_ids: list[int] = Field(default_factory=list)
    direction: Literal["support", "contradict", "context"] = "support"
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class StageOutput(BaseModel):
    """Stage analyzer return type — pre-DB write contract."""
    model_config = ConfigDict(extra="forbid")

    stage_type: Literal["market", "news", "fundamentals", "social"]
    verdict: StageVerdict
    confidence: int = Field(ge=0, le=100)
    signals: MarketSignals | NewsSignals | FundamentalsSignals | SocialSignals
    raw_payload: dict | None = None
    source_freshness: SourceFreshness | None = None
    model_name: str | None = None
    prompt_version: str | None = None
    snapshot_at: datetime | None = None


class PriceAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    appropriate_buy_min: float | None = None
    appropriate_buy_max: float | None = None
    appropriate_sell_min: float | None = None
    appropriate_sell_max: float | None = None
    buy_hope_min: float | None = None
    buy_hope_max: float | None = None
    sell_target_min: float | None = None
    sell_target_max: float | None = None


class SummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: SummaryDecision
    confidence: int = Field(ge=0, le=100)
    bull_arguments: list[BullBearArgument]
    bear_arguments: list[BullBearArgument]
    price_analysis: PriceAnalysis | None = None
    reasons: list[str] = Field(default_factory=list, max_length=10)
    detailed_text: str | None = None
    warnings: list[str] = Field(default_factory=list)
    model_name: str | None = None
    prompt_version: str | None = None
    raw_payload: dict | None = None
    token_input: int | None = None
    token_output: int | None = None
