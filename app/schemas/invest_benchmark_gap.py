"""ROB-271 — read-only Toss/Naver benchmark data-sourcing gap matrix schemas.

Product-facing types live here so the existing CoverageState/ActionReadinessState
contracts remain untouched. The gap matrix is an additive read-only view layered
on top of /invest/api/coverage and /invest/api/kr/action-readiness.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.invest_coverage import CoverageMarket

BenchmarkProvider = Literal[
    "toss",
    "naver",
    "internal",
    "kis",
    "upbit",
    "news_ingestor",
]

SourceRole = Literal[
    "source_of_truth",
    "broker_authority",
    "owned_read_model",
    "reference",
    "candidate",
    "benchmark_only",
    "excluded",
    "unsupported",
]

CoverageProductStatus = Literal[
    "covered",
    "partial",
    "stale",
    "missing",
    "candidate_unwired",
    "benchmark_only",
    "intentionally_excluded",
    "unsupported",
    "blocked_by_auth_or_policy",
]

BenchmarkGapPriority = Literal["P0", "P1", "P2", "P3"]

BenchmarkGapDataKind = Literal[
    "raw",
    "snapshot",
    "derived",
    "ui_only",
    "account",
    "broker_authority",
    "reference",
]


class BenchmarkGapRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Required (MVP)
    id: str
    featureArea: str
    benchmarkProvider: BenchmarkProvider
    benchmarkSurface: str
    benchmarkLabelKo: str
    sourceRole: SourceRole
    coverageStatus: CoverageProductStatus
    priority: BenchmarkGapPriority
    whyNeeded: str
    nextAction: str

    # Optional (expansion seams)
    benchmarkUrl: str | None = None
    autoTraderSurface: str | None = None
    autoTraderApi: str | None = None
    autoTraderReadModel: str | None = None
    autoTraderTable: str | None = None
    dataKind: BenchmarkGapDataKind | None = None
    freshnessAt: datetime | None = None
    gapReason: str | None = None
    relatedLinearIssue: str | None = None
    newIssueCandidate: bool = False
    notes: list[str] = Field(default_factory=list)


class NextSourcingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rowId: str
    priority: BenchmarkGapPriority
    featureArea: str
    benchmarkProvider: BenchmarkProvider
    gap: str
    whyItMatters: str
    currentStatus: CoverageProductStatus
    nextAction: str
    currentAutoTrader: str | None = None
    relatedLinearIssue: str | None = None
    newIssueCandidate: bool = False


class BenchmarkGapMatrixSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    totalRows: int
    byStatus: dict[str, int] = Field(default_factory=dict)
    byPriority: dict[str, int] = Field(default_factory=dict)
    byProvider: dict[str, int] = Field(default_factory=dict)


class BenchmarkGapMatrixResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: CoverageMarket
    asOf: datetime
    rows: list[BenchmarkGapRow]
    nextCandidates: list[NextSourcingCandidate]
    summary: BenchmarkGapMatrixSummary
    sourcePolicy: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
