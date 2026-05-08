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
    warnings: list[str] = Field(default_factory=list)


class ScreenerPresetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presets: list[ScreenerPreset]
    selectedPresetId: str | None = None


class ScreenerResultsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presetId: str
    title: str
    description: str
    filterChips: list[ScreenerFilterChip]
    metricLabel: str
    results: list[ScreenerResultRow]
    warnings: list[str] = Field(default_factory=list)
