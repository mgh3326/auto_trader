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
ScreenerDataSourceKind = Literal[
    "upbit_official",
    "tvscreener_upbit",
    "mcp_screen_stocks",
    "naver_reference",
    "coingecko_reference",
    "external_reference",
    "snapshot_cache",
]
ScreenerSourceState = Literal[
    "supported",
    "cached",
    "reference_only",
    "partial",
    "unavailable",
    "fallback",
]
ScreenerRiskSeverity = Literal["info", "warning", "danger"]
# ROB-359 Scope B — catalog provenance + Toss parity honesty.
# presetOrigin distinguishes a Toss 골라보기 baseline preset from an
# auto_trader-original preset; parityStatus marks how closely a toss_parity
# preset matches Toss semantics (auto_trader_original presets leave it None).
ScreenerPresetOrigin = Literal["toss_parity", "auto_trader_original"]
ScreenerParityStatus = Literal["full", "partial", "mismatch"]


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
    # ROB-359 Scope B (additive, optional). presetOrigin lets the catalog
    # separate Toss-parity presets from auto_trader-original ones; parityStatus
    # + parityNote surface honest divergence (partial/mismatch) without
    # fabricating results. None defaults keep existing constructions valid.
    presetOrigin: ScreenerPresetOrigin | None = None
    parityStatus: ScreenerParityStatus | None = None
    parityNote: str | None = None


class ScreenerSourceContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: ScreenerDataSourceKind
    label: str
    state: ScreenerSourceState
    fetchedAt: str | None = None
    detail: str | None = None


class ScreenerRiskContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    label: str
    severity: ScreenerRiskSeverity = "info"
    source: ScreenerDataSourceKind | None = None


class ScreenerCandidateContext(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scoreLabel: str | None = None
    reasons: list[str] = Field(default_factory=list)
    source: ScreenerDataSourceKind | None = None


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
    sourceContext: list[ScreenerSourceContext] = Field(default_factory=list)
    riskContext: list[ScreenerRiskContext] = Field(default_factory=list)
    candidateContext: ScreenerCandidateContext | None = None


class ScreenerPresetsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    presets: list[ScreenerPreset]
    selectedPresetId: str | None = None


class ScreenerFreshnessPrimary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["screener_snapshot", "live", "fallback"]
    snapshotDate: str | None = None
    computedAt: str | None = None
    asOfLabel: str
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"]
    source: str | None = None


class ScreenerFreshnessDependency(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["investor_flow"]
    snapshotDate: str | None = None
    collectedAt: str | None = None
    lagLabel: str | None = None
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"]
    source: str | None = None


class ScreenerFreshness(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fetchedAt: str
    asOfLabel: str
    relativeLabel: str
    cacheHit: bool
    source: Literal["live", "cached", "previous_session"]
    dataState: Literal["fresh", "partial", "stale", "missing", "fallback"] = "missing"
    # New (additive, optional) fields — see ROB-277 plan §D1.
    servedAt: str | None = None
    servedRelativeLabel: str | None = None
    primary: ScreenerFreshnessPrimary | None = None
    dependencies: list[ScreenerFreshnessDependency] = Field(default_factory=list)
    overallState: Literal["fresh", "partial", "stale", "missing", "fallback"] | None = (
        None
    )


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
    sources: list[ScreenerSourceContext] = Field(default_factory=list)
