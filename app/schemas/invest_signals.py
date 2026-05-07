"""ROB-143 — /invest/api/signals schema."""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SignalTab = Literal["mine", "kr", "us", "crypto"]
SignalMarket = Literal["kr", "us", "crypto"]
SignalSource = Literal["analysis", "issue", "brief"]
DecisionLabel = Literal["buy", "hold", "sell", "watch", "neutral"]
Severity = Literal["low", "medium", "high"]
RelationKind = Literal["held", "watchlist", "both", "none"]


class SignalRelatedSymbol(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str
    market: SignalMarket
    displayName: str


class SignalCard(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    source: SignalSource
    title: str
    market: SignalMarket
    decisionLabel: DecisionLabel | None = None
    confidence: int | None = None
    severity: Severity | None = None
    summary: str | None = None
    generatedAt: datetime
    relatedSymbols: list[SignalRelatedSymbol] = Field(default_factory=list)
    relatedIssueIds: list[str] = Field(default_factory=list)
    supportingNewsIds: list[int] = Field(default_factory=list)
    rationale: str | None = None
    relation: RelationKind = "none"


class SignalsMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")
    emptyReason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class SignalsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tab: SignalTab
    asOf: datetime
    items: list[SignalCard] = Field(default_factory=list)
    meta: SignalsMeta = Field(default_factory=SignalsMeta)
