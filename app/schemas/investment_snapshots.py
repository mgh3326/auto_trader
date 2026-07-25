# app/schemas/investment_snapshots.py
"""ROB-269 Phase 1 — Pydantic DTOs for snapshot foundation."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SnapshotPurpose = Literal[
    "report_generation", "scheduled_refresh", "manual_refresh", "reviewer_requested"
]
SnapshotMarket = Literal["kr", "us", "crypto"]
SnapshotAccountScope = Literal["kis_live", "kis_mock", "alpaca_paper", "upbit_live"]
SnapshotRunStatus = Literal["running", "completed", "partial", "failed"]
SnapshotRequestedBy = Literal["hermes", "user", "scheduler", "claude_code", "reviewer"]
SnapshotKind = Literal[
    "portfolio",
    "market",
    "news",
    "symbol",
    "candidate_universe",
    "browser_probe",
    "invest_page",
    "journal",
    "watch_context",
    "naver_remote_debug",
    "toss_remote_debug",
    "llm_input_frozen",
    "pending_orders",
    "validated_run_card",
    "kr_market_ranking",
    "investor_flow",
]
SourceKind = Literal[
    "kis_mcp",
    "auto_trader_mcp",
    "invest_api",
    "naver_remote_debug",
    "toss_remote_debug",
    "combined",
    "news_ingestor",
    "manual",
    "domain_ref",
]
FreshnessStatus = Literal["fresh", "soft_stale", "hard_stale", "partial", "unavailable"]
BundleStatus = Literal["complete", "partial", "stale_fallback", "failed"]
BundleItemRole = Literal["required", "optional", "fallback", "conflict_evidence"]


class SnapshotRunCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: SnapshotPurpose
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    requested_by: SnapshotRequestedBy
    policy_version: str
    policy_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    refresh_reason: str | None = None
    run_metadata: dict[str, Any] = Field(default_factory=dict)


class SnapshotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_uuid: uuid.UUID
    snapshot_kind: SnapshotKind
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    symbol: str | None = None
    source_table: str | None = None
    source_id: int | None = None
    source_uri: str | None = None
    source_kind: SourceKind
    payload_json: dict[str, Any] = Field(default_factory=dict)
    source_timestamps_json: dict[str, Any] = Field(default_factory=dict)
    coverage_json: dict[str, Any] = Field(default_factory=dict)
    errors_json: dict[str, Any] = Field(default_factory=dict)
    as_of: dt.datetime
    valid_until: dt.datetime | None = None
    freshness_status: FreshnessStatus

    @model_validator(mode="after")
    def _source_ref_triple_consistent(self) -> SnapshotCreate:
        triple = (self.source_table, self.source_id, self.source_uri)
        nulls = sum(1 for v in triple if v is None)
        if nulls not in (0, 3):
            raise ValueError(
                "source_table / source_id / source_uri must all be set or all None"
            )
        return self

    @model_validator(mode="after")
    def _domain_ref_requires_source_triple(self) -> SnapshotCreate:
        if self.source_kind == "domain_ref" and self.source_table is None:
            raise ValueError(
                "source_kind='domain_ref' requires the source_table/source_id/source_uri triple"
            )
        return self


class BundleCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    purpose: str
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None = None
    policy_version: str
    policy_snapshot_json: dict[str, Any] = Field(default_factory=dict)
    as_of: dt.datetime
    status: BundleStatus
    coverage_summary: dict[str, Any] = Field(default_factory=dict)
    freshness_summary: dict[str, Any] = Field(default_factory=dict)
    idempotency_discriminator: str | None = None
    """Optional persisted idempotency-key component for create-new calls."""


class BundleItemCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_uuid: uuid.UUID
    role: BundleItemRole
