"""ROB-216 — strict read-only /invest FX·macro dashboard contract."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FxDashboardDataState = Literal["fresh", "partial", "missing", "stale", "error"]
FxDashboardTone = Literal["up", "down", "flat", "unknown"]
DefenseSignalState = Literal[
    "none",
    "watch",
    "elevated",
    "after_verification_required",
]
DefenseSignalConfidence = Literal["low", "medium", "high"]
FxDisclaimerSeverity = Literal["info", "caution", "warning"]
FxDashboardThresholdState = Literal["watch", "near", "breached"]


class FxDashboardSourceFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: str
    label: str
    dataState: FxDashboardDataState
    updatedAt: datetime | None = None
    staleAfterMinutes: int | None = Field(default=None, ge=0)
    warning: str | None = None


class FxDashboardDisclaimer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    severity: FxDisclaimerSeverity
    textKo: str


class FxDashboardQuoteMetric(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    label: str | None = None
    value: float | None = None
    spot: float | None = None
    change: float | None = None
    changePct: float | None = None
    tone: FxDashboardTone = "unknown"
    updatedAt: datetime | None = None
    dataState: FxDashboardDataState | None = None
    source: str


class FxDashboardThreshold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    level: float
    label: str
    distancePct: float
    state: FxDashboardThresholdState


class FxDashboardEvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str
    labelKo: str
    value: str | None = None
    source: str
    dataState: FxDashboardDataState


class FxDashboardDefenseSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: DefenseSignalState
    score: int = Field(ge=0, le=100)
    confidence: DefenseSignalConfidence
    labelKo: str
    summaryKo: str
    reasonsKo: list[str] = Field(default_factory=list)
    evidence: list[FxDashboardEvidenceItem] = Field(default_factory=list)
    notConfirmedIntervention: bool
    needsAfterVerification: bool


class FxDashboardCollectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    symbol: str
    label: str
    value: float | None = None
    changePct: float | None = None
    dataState: FxDashboardDataState
    source: str


class FxDashboardForeignFlowItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str | None = None
    source: str
    dataState: FxDashboardDataState


class FxDashboardForeignFlowSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataState: FxDashboardDataState
    summaryKo: str
    items: list[FxDashboardForeignFlowItem] = Field(default_factory=list)


class FxDashboardNewsItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    source: str
    publishedAt: datetime | None = None
    url: str | None = None
    dataState: FxDashboardDataState = "fresh"


class FxDashboardNewsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataState: FxDashboardDataState
    items: list[FxDashboardNewsItem] = Field(default_factory=list)
    warning: str | None = None


class FxDashboardEventItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    startsAt: datetime | None = None
    source: str
    dataState: FxDashboardDataState = "fresh"


class FxDashboardEventsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataState: FxDashboardDataState
    items: list[FxDashboardEventItem] = Field(default_factory=list)
    warning: str | None = None


class FxDashboardAfterVerification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataState: FxDashboardDataState
    officialEvidence: list[FxDashboardEvidenceItem] = Field(default_factory=list)
    dealerEvidence: list[FxDashboardEvidenceItem] = Field(default_factory=list)
    ndfEvidence: list[FxDashboardEvidenceItem] = Field(default_factory=list)
    summaryKo: str


class FxDashboardResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asOf: datetime
    dataState: FxDashboardDataState
    warnings: list[str] = Field(default_factory=list)
    disclaimers: list[FxDashboardDisclaimer] = Field(default_factory=list)
    sourceFreshness: list[FxDashboardSourceFreshness] = Field(default_factory=list)
    usdKrw: FxDashboardQuoteMetric
    thresholds: list[FxDashboardThreshold] = Field(default_factory=list)
    defenseSignal: FxDashboardDefenseSignal
    globalDollar: list[FxDashboardCollectionItem] = Field(default_factory=list)
    krwCrosses: list[FxDashboardCollectionItem] = Field(default_factory=list)
    foreignFlow: FxDashboardForeignFlowSection
    news: FxDashboardNewsSection
    events: FxDashboardEventsSection
    afterVerification: FxDashboardAfterVerification
