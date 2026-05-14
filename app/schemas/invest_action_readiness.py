"""ROB-256 — read-only KR action-report data readiness contract."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.invest_coverage import (
    CoverageActionability,
    CoverageState,
    InvestCoverageCounts,
)

ActionReadinessState = Literal[
    "ready",
    "degraded",
    "blocked",
    "missing",
    "unsupported",
    "unknown",
]
ActionReadinessAuthority = Literal[
    "kis_live_broker",
    "auto_trader_read_model",
    "manual_or_paper_reference",
    "external_reference",
    "unsupported",
]
ActionReportImpact = Literal[
    "none",
    "degrades_report",
    "blocks_buy_report",
    "blocks_sell_report",
    "blocks_all_action_reports",
]


class ActionReadinessLink(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    href: str


class ActionReadinessFamily(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    labelKo: str
    category: str
    state: ActionReadinessState
    impact: ActionReportImpact
    authority: ActionReadinessAuthority
    sourceOfTruth: str
    references: list[str] = Field(default_factory=list)
    latestAt: datetime | None = None
    latestDate: date | None = None
    counts: InvestCoverageCounts | None = None
    coverageState: CoverageState | None = None
    actionability: CoverageActionability
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    links: list[ActionReadinessLink] = Field(default_factory=list)


class KrActionReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: Literal["kr"] = "kr"
    asOf: datetime
    symbol: str | None = None
    overallState: ActionReadinessState
    canGenerateBuyReport: bool
    canGenerateSellReport: bool
    families: list[ActionReadinessFamily]
    blockers: list[str] = Field(default_factory=list)
    degradedSignals: list[str] = Field(default_factory=list)
    sourcePolicy: list[str]
    notes: list[str] = Field(default_factory=list)
