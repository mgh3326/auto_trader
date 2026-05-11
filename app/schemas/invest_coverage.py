"""ROB-192 — read-only /invest Toss-parity data coverage schemas."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CoverageState = Literal[
    "fresh",
    "stale",
    "partial",
    "missing",
    "unsupported",
    "error",
    "provider_unwired",
]
CoverageMarket = Literal["kr", "us", "crypto", "all"]


class InvestCoverageCounts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected: int | None = None
    fresh: int = 0
    stale: int = 0
    missing: int = 0
    partial: int = 0
    total: int = 0


class InvestCoverageSurface(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface: str
    label: str
    state: CoverageState
    market: str | None = None
    sourceOfTruth: str
    reference: str = "toss"
    latestAt: datetime | None = None
    latestDate: date | None = None
    counts: InvestCoverageCounts = Field(default_factory=InvestCoverageCounts)
    staleAfterHours: int | None = None
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class InvestCoverageSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    market: str
    surfaces: dict[str, CoverageState] = Field(default_factory=dict)
    latestDates: dict[str, date | None] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class InvestCoverageResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: CoverageMarket
    asOf: datetime
    tradingDate: date
    states: list[CoverageState]
    surfaces: list[InvestCoverageSurface]
    symbols: list[InvestCoverageSymbol] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
