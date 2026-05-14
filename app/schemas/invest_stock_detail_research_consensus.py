"""ROB-249 — stock detail research consensus transport schema.

This schema is citation-only. It intentionally references
``ResearchReportCitation`` and never exposes report body/PDF/raw payload text.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.research_reports import ResearchReportCitation

StockDetailResearchConsensusState = Literal[
    "ready", "partial", "missing", "unsupported", "error"
]
StockDetailResearchConsensusDataState = Literal[
    "fresh", "stale", "missing", "unsupported", "error"
]
StockDetailResearchConsensusEmptyReason = Literal[
    "no_analyst_consensus_or_research_reports",
    "market_unsupported",
    "provider_error",
]
StockDetailResearchConsensusSourceOfTruth = Literal[
    "analyst_opinions_and_research_reports",
    "analyst_opinions",
    "research_reports",
    "none",
]


class StockDetailAnalystConsensus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str | None = None
    buyCount: int = 0
    holdCount: int = 0
    sellCount: int = 0
    strongBuyCount: int = 0
    totalCount: int = 0
    avgTargetPrice: float | None = None
    medianTargetPrice: float | None = None
    minTargetPrice: float | None = None
    maxTargetPrice: float | None = None
    upsidePct: float | None = None
    currentPrice: float | None = None


class StockDetailResearchFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    isReady: bool
    isStale: bool
    latestRunUuid: str | None = None
    latestFinishedAt: datetime | None = None
    latestReportCount: int = 0
    maxAgeHours: int


class StockDetailResearchConsensusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: Literal["kr", "us"]
    displayName: str
    state: StockDetailResearchConsensusState
    dataState: StockDetailResearchConsensusDataState
    emptyReason: StockDetailResearchConsensusEmptyReason | None = None
    warnings: list[str] = Field(default_factory=list)
    sourceOfTruth: StockDetailResearchConsensusSourceOfTruth = "none"
    asOf: datetime
    stale: bool = False
    consensus: StockDetailAnalystConsensus | None = None
    citations: list[ResearchReportCitation] = Field(default_factory=list)
    freshness: StockDetailResearchFreshness
