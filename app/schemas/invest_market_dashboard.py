"""ROB-198 — read-only /invest market dashboard schemas."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MarketDashboardState = Literal["fresh", "partial", "missing", "error"]
MarketDashboardSectionId = Literal[
    "kr_market",
    "global_indices",
    "fx_macro",
    "crypto_market",
]


class MarketDashboardMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str | None = None
    change: float | None = None
    changePct: float | None = None
    tone: Literal["up", "down", "flat", "unknown"] = "unknown"
    unit: str | None = None
    source: str
    symbol: str | None = None
    href: str | None = None
    stale: bool = False
    warning: str | None = None
    dataState: str | None = None
    dataStateReason: str | None = None
    quoteAsOf: datetime | None = None
    quoteLagSeconds: int | None = None


class MarketDashboardSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: MarketDashboardSectionId
    title: str
    subtitle: str
    reference: str = "naver"
    state: MarketDashboardState
    sourceOfTruth: str
    updatedAt: datetime | None = None
    staleAfterMinutes: int | None = None
    metrics: list[MarketDashboardMetric] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class MarketDashboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asOf: datetime
    state: MarketDashboardState
    sections: list[MarketDashboardSection]
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
