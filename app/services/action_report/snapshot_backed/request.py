"""Request / response schemas for the snapshot-backed report generator."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.investment_reports import IngestReportItem, MarketSessionLiteral
from app.schemas.investment_snapshots import SnapshotRequestedBy

GeneratorMarketLiteral = Literal["kr", "us", "crypto"]
GeneratorAccountScopeLiteral = Literal["kis_live", "upbit_live"]
GeneratorStatusLiteral = Literal["draft", "published"]


class ReportGenerationRequest(BaseModel):
    """Request envelope for :class:`SnapshotBackedReportGenerator.generate`.

    Supported canonical pairs (enforced by the generator at runtime):

    * ``kr / kis_live`` — KIS domestic stock account.
    * ``us / kis_live`` — KIS overseas (US) stock account (ROB-297).
    * ``crypto / upbit_live`` — Upbit spot.

    ``account_scope`` stays a single canonical literal — KR vs US is
    disambiguated by ``market`` per ROB-297 guardrail #2. No
    ``kis_overseas_live`` alias is introduced.
    """

    model_config = ConfigDict(extra="forbid")

    market: GeneratorMarketLiteral
    account_scope: GeneratorAccountScopeLiteral
    # ROB-352 — constrained to the same vocabulary as the persisted layer
    # (IngestReportRequest) so an invalid session fails fast at request
    # validation instead of deep inside the ingest-request build.
    market_session: MarketSessionLiteral | None = None
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

    # ROB-347 — US new-buy budget basis policy. Default available_usd: USD
    # buying power gates buy_review. USD<=0 demotes buy_review → watch_only with
    # budget_gap/fx_required/operator_budget_required (never a silent buy). KRW
    # is reference-only; no KRW→USD fabrication.
    budget_basis: Literal[
        "available_usd", "krw_orderable_reference", "operator_budget_override"
    ] = "available_usd"
    operator_budget_override_usd: Decimal | None = None

    # ROB-287 — fail-closed legacy field. The ROB-279 in-process LLM
    # composition path (Gemini-backed FinalComposer + LLM reducer stages)
    # was removed; LLM reasoning/composition now belongs to Hermes via the
    # `investment_report_get_hermes_context` / Hermes-result-ingest contract.
    # ``True`` is rejected at validation time so callers cannot accidentally
    # re-enable an in-process LLM path.
    auto_compose: bool = False

    # ROB-278 — operator user_id for live-account read paths (e.g. KIS
    # holdings/cash). ``None`` keeps broker-backed collectors fail-closed.
    user_id: int | None = None

    # ROB-352 — deterministic regeneration semantics. Default is REUSE: when a
    # report already exists for this deterministic idempotency key, the stored
    # row is returned unchanged (the generator never emits a freshly-computed,
    # unstored payload). Set ``overwrite_existing=True`` (with a reason) to
    # transactionally replace the stored report + items in place. Mutating
    # report_type/created_by_profile to force a new row is NOT supported.
    overwrite_existing: bool = False
    overwrite_reason: str | None = None

    # ROB-278 Phase 2 — when True, populate ``items`` from a deterministic
    # evidence-driven auto-emitter (portfolio + symbol quote + candidate +
    # news + journal/watch). Items emit with operation="review" +
    # apply_policy="requires_user_approval"; mutation paths remain
    # unreachable. Per ROB-287, this proposer remains a deterministic,
    # explicit-flag-only path and never co-runs with Hermes composition
    # against the same bundle.
    auto_emit_from_evidence: bool = False

    @model_validator(mode="after")
    def _require_overwrite_reason(self) -> ReportGenerationRequest:
        # ROB-352 — a destructive in-place overwrite must carry a non-empty
        # reason for the audit trail; it is recorded in report_metadata.
        if self.overwrite_existing and not (
            self.overwrite_reason and self.overwrite_reason.strip()
        ):
            raise ValueError(
                "overwrite_reason is required (non-empty) when overwrite_existing=True"
            )
        return self

    @field_validator("auto_compose")
    @classmethod
    def _reject_auto_compose(cls, value: bool) -> bool:
        if value:
            raise ValueError(
                "auto_compose=True is no longer supported (ROB-287). "
                "LLM composition is owned by Hermes; use the Hermes context "
                "export + ingest contract instead."
            )
        return value


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

    # ROB-352 — True when the response reflects an existing stored report that
    # was returned unchanged (default reuse path). When True, callers should
    # pass overwrite_existing=True + overwrite_reason to regenerate.
    reused_existing: bool = False

    # ROB-318 Phase 3 (PR-A) — deterministic classification of why the report
    # concludes no-action: data_insufficient | stale_gated | real_no_action,
    # or ``None`` when an action is allowed. ``{kind, blocking_sources,
    # reason_ko}``. Computed deterministically; Hermes owns the prose. In PR-A
    # this is a response-only field (ephemeral); PR-B persists it.
    why_no_action: dict[str, Any] | None = None
