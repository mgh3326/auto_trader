"""ROB-147 — read-only DTOs for /invest/api/screener/*.

All fields are display-ready labels. Numeric values are intentionally pre-formatted
so the React layer can render rows without re-running locale logic. When a metric
is unavailable for a row, set the *Label field to "-" and surface a warning string.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ScreenerMarket = Literal["kr", "us", "crypto"]
ChangeDirection = Literal["up", "down", "flat"]
InvestorFlowChipTone = Literal[
    "double_buy",
    "double_sell",
    "foreign_buy",
    "foreign_sell",
    "institution_buy",
    "institution_sell",
    "neutral",
]
InvestorFlowChipState = Literal["fresh", "stale", "missing"]


class ScreenerInvestorFlowChip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    tone: InvestorFlowChipTone
    dataState: InvestorFlowChipState
    snapshotDate: str | None = None


class ScreenerFilterChip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    detail: str | None = None


class ScreenerPreset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    description: str
    badges: list[str] = Field(default_factory=list)
    filterChips: list[ScreenerFilterChip] = Field(default_factory=list)
    metricLabel: str
    market: ScreenerMarket = "kr"


class ScreenerResultRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rank: int = Field(ge=1)
    symbol: str
    market: ScreenerMarket
    name: str
    logoUrl: str | None = None
    isWatched: bool = False
    priceLabel: str
    changePctLabel: str
    changeAmountLabel: str
    changeDirection: ChangeDirection
    category: str
    marketCapLabel: str
    volumeLabel: str
    analystLabel: str
    metricValueLabel: str
    investorFlowChip: ScreenerInvestorFlowChip | None = None
    warnings: list[str] = Field(default_factory=list)


class ScreenerPresetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presets: list[ScreenerPreset]
    selectedPresetId: str | None = None


class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str
    asOfLabel: str
    relativeLabel: str
    cacheHit: bool
    source: Literal["live", "cached", "previous_session"]
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"] = "missing"


class ScreenerResultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presetId: str
    title: str
    description: str
    filterChips: list[ScreenerFilterChip]
    metricLabel: str
    results: list[ScreenerResultRow]
    warnings: list[str] = Field(default_factory=list)
    freshness: ScreenerFreshness
