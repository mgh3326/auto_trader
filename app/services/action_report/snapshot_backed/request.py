"""Request / response schemas for the snapshot-backed report generator."""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.investment_reports import IngestReportItem
from app.schemas.investment_snapshots import SnapshotRequestedBy

GeneratorMarketLiteral = Literal["kr", "crypto"]
GeneratorAccountScopeLiteral = Literal["kis_live", "upbit_live"]
GeneratorStatusLiteral = Literal["draft", "published"]


class ReportGenerationRequest(BaseModel):
    """Request envelope for :class:`SnapshotBackedReportGenerator.generate`.

    Only ``kr / kis_live`` and ``crypto / upbit_live`` are supported in this
    PR. The generator validates the pairing at runtime; expanding to US is
    intentionally a follow-up.
    """

    model_config = ConfigDict(extra="forbid")

    market: GeneratorMarketLiteral
    account_scope: GeneratorAccountScopeLiteral
    market_session: str | None = None
    policy_version: str = "intraday_action_report_v1"
    execution_mode: Literal["advisory_only"] = "advisory_only"
    status: GeneratorStatusLiteral = "published"
    requested_by: SnapshotRequestedBy = "claude_code"

    report_type: str = "snapshot_backed_advisory_v1"
    generator_version: str = "v2-snapshot-backed"
    created_by_profile: str
    title: str
    summary: str
    kst_date: str
    risk_summary: str | None = None
    thesis_text: str | None = None
    no_action_note: str | None = None

    items: list[IngestReportItem] = Field(default_factory=list)
    previous_report_uuid: UUID | None = None
    valid_until: dt.datetime | None = None
    published_at: dt.datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # Snapshot-bundle inputs forwarded to SnapshotBundleEnsureService.
    symbols: list[str] | None = None
    candidate_limit: int | None = None

    # ROB-279 — when True, the generator synthesizes report fields and items
    # via the staged snapshot-backed pipeline instead of using provided ones.
    auto_compose: bool = False


class ReportGenerationResponse(BaseModel):
    """Result envelope for the generator. JSON-safe."""

    model_config = ConfigDict(extra="forbid")

    report_uuid: UUID
    snapshot_bundle_uuid: UUID
    snapshot_policy_version: str
    snapshot_coverage_summary: dict[str, Any]
    snapshot_freshness_summary: dict[str, Any]
    source_conflicts: dict[str, Any]
    unavailable_sources: dict[str, Any]
    items_count: int
    warnings: list[str]
    bundle_status: str
    bundle_reused: bool
    stale_gate: dict[str, Any]
