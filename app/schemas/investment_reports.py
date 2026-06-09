"""ROB-265 — Pydantic v2 request schemas for the investment_* service layer.

These mirror the locked product decisions:

* Advisory-only invariants: ``kis_live`` account + ``nxt`` session both
  force ``execution_mode='advisory_only'``.
* Watch items: ``watch_condition`` and ``valid_until`` are both required
  when ``item_kind='watch'``.
* Schema validators run BEFORE we hit the DB, so callers get a clean
  Pydantic ValidationError instead of an IntegrityError.

Defense in depth — the DB CHECK constraints from Plan 1 are the
authoritative enforcement; these are early rejection for cleaner errors.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.execution_contracts import AccountMode
from app.schemas.investment_snapshots import (
    BundleItemRole,
    BundleStatus,
    FreshnessStatus,
    SnapshotAccountScope,
    SnapshotKind,
    SnapshotMarket,
    SourceKind,
)

# Enum-equivalent Literals — match the DB CHECK constraints from
# alembic/versions/20260518_rob265_add_investment_reports.py exactly.
MarketLiteral = Literal["kr", "us", "crypto"]
MarketSessionLiteral = Literal["regular", "nxt", "pre", "post", "24x7"]
AccountScopeLiteral = Literal["kis_live", "kis_mock", "alpaca_paper", "upbit_live"]
ExecutionModeLiteral = Literal["advisory_only", "mock_preview"]
ReportStatusLiteral = Literal["draft", "published", "decided", "expired", "superseded"]
# ROB-455 — the lifecycle targets an operator may transition a report TO. draft /
# published are entry states (set at create), not transition targets here.
ReportStatusTransitionLiteral = Literal["superseded", "decided", "expired"]

ItemKindLiteral = Literal["action", "watch", "risk"]
ItemSideLiteral = Literal["buy", "sell"]
ItemIntentLiteral = Literal[
    "buy_review",
    "sell_review",
    "risk_review",
    "trend_recovery_review",
    "rebalance_review",
]
TargetKindLiteral = Literal["asset", "index", "fx"]
ItemStatusLiteral = Literal[
    "proposed", "approved", "denied", "deferred", "activated", "expired"
]

WatchMetricLiteral = Literal["price", "rsi", "trade_value"]
WatchOperatorLiteral = Literal["above", "below"]
WatchClauseOpLiteral = Literal["above", "below", "between"]
WatchCombineLiteral = Literal["and"]
WatchActionModeLiteral = Literal[
    "notify_only", "preview_only", "approval_required", "auto_execute_mock"
]

DecisionVerbLiteral = Literal["approve", "deny", "defer", "skip", "partial_approve"]

# ROB-274 — proposal lifecycle literals. ``operation=None`` is the legacy
# shape and is treated as 'create' by the DB CHECK constraints (see
# alembic/versions/20260520_rob274_p1_*.py). ``apply_policy`` is locked to
# a single value in this PR; broader policy modes land in a follow-up.
OperationLiteral = Literal["create", "modify", "cancel", "keep", "replace", "review"]
ApplyPolicyLiteral = Literal["requires_user_approval"]
TargetRefTypeLiteral = Literal["investment_watch_alert", "broker_order", "ambiguous"]


class WatchConditionClause(BaseModel):
    """One condition clause. above/below use ``threshold``; between uses low/high."""

    metric: WatchMetricLiteral
    op: WatchClauseOpLiteral
    threshold: Decimal | None = None
    low: Decimal | None = None
    high: Decimal | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _validate_clause(self) -> WatchConditionClause:
        if self.op in ("above", "below"):
            if self.threshold is None:
                raise ValueError(f"op={self.op} requires threshold")
            if self.low is not None or self.high is not None:
                raise ValueError(f"op={self.op} must not set low/high")
        elif self.op == "between":
            if self.low is None or self.high is None:
                raise ValueError("op=between requires low and high")
            if self.low > self.high:
                raise ValueError("op=between requires low <= high")
            if self.threshold is not None:
                raise ValueError("op=between must not set threshold")
        return self


def _derive_condition_key(clauses: list[WatchConditionClause]) -> str:
    """Deterministic dedup key. Single above/below clause keeps legacy str(threshold)."""
    if len(clauses) == 1 and clauses[0].op in ("above", "below"):
        return str(clauses[0].threshold)
    parts: list[str] = []
    for c in clauses:
        if c.op == "between":
            parts.append(f"{c.metric}:between:{c.low}-{c.high}")
        else:
            parts.append(f"{c.metric}:{c.op}:{c.threshold}")
    return "and(" + ",".join(parts) + ")"


class WatchConditionPayload(BaseModel):
    """Embedded condition for a watch item. Persisted as JSONB.

    Two accepted input shapes, both normalized to ``conditions``:
    - legacy flat: ``metric`` + ``operator`` + ``threshold``
    - v2: ``conditions=[{metric, op, threshold|low/high}]`` + ``combine``
    """

    # legacy flat (optional)
    metric: WatchMetricLiteral | None = None
    operator: WatchOperatorLiteral | None = None
    threshold: Decimal | None = None
    threshold_key: str | None = None
    target_kind: TargetKindLiteral = "asset"
    action_mode: WatchActionModeLiteral = "notify_only"
    # v2
    conditions: list[WatchConditionClause] | None = None
    combine: WatchCombineLiteral = "and"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _normalize(self) -> WatchConditionPayload:
        if self.conditions is None:
            if self.metric is None or self.operator is None or self.threshold is None:
                raise ValueError(
                    "watch_condition requires either conditions[] or "
                    "metric+operator+threshold"
                )
            self.conditions = [
                WatchConditionClause(
                    metric=self.metric, op=self.operator, threshold=self.threshold
                )
            ]
        elif not self.conditions:
            raise ValueError("conditions must be non-empty")
        if self.threshold_key is None:
            self.threshold_key = _derive_condition_key(self.conditions)
        return self


class TargetRefPayload(BaseModel):
    """Reference to the existing operational state an item proposes to act on.

    ROB-274 — used by modify/cancel/keep/review/replace proposals to point
    at the source-of-truth record (an ``investment_watch_alert`` row, a
    broker order id, or an ambiguous candidate list when the producer
    couldn't resolve a single target).
    """

    type: TargetRefTypeLiteral
    id: str | None = None
    status: str | None = None
    broker: str | None = None
    raw: dict[str, Any] | None = None
    candidates: list[dict[str, Any]] | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _ambiguous_needs_candidates(self) -> TargetRefPayload:
        if self.type == "ambiguous":
            if not self.candidates:
                raise ValueError(
                    "target_ref.type='ambiguous' requires non-empty candidates"
                )
        else:
            if self.id is None:
                raise ValueError(
                    "target_ref.id is required for non-ambiguous target_ref"
                )
        return self


class MaxActionPayload(BaseModel):
    """Structured order params a watch trigger proposes. Consumed by ROB-402.

    ``extra='allow'`` preserves legacy keys (e.g. ``notional_usd`` used by
    mock_preview). The live auto-execute block is enforced by ROB-402 on the
    (action_mode, account_mode) combination, not here.
    """

    side: ItemSideLiteral
    quantity: Decimal | None = None
    notional: Decimal | None = None
    limit_price: Decimal | None = None
    account_mode: AccountMode

    model_config = ConfigDict(extra="allow")

    @model_validator(mode="after")
    def _xor_quantity_notional(self) -> MaxActionPayload:
        has_qty = self.quantity is not None
        has_notional = self.notional is not None
        if has_qty == has_notional:
            raise ValueError("max_action requires exactly one of quantity or notional")
        return self


class IngestReportItem(BaseModel):
    """One proposal item attached to an ingested report.

    ``client_item_key`` is a caller-supplied disambiguator used by the
    item idempotency-key composer. It must be unique within a single
    report bundle. Use it to disambiguate items that share natural
    fields — multiple risk items, scoped buys on the same symbol, etc.
    """

    client_item_key: str = Field(min_length=1)
    item_kind: ItemKindLiteral
    operation: OperationLiteral | None = None
    symbol: str | None = None
    side: ItemSideLiteral | None = None
    intent: ItemIntentLiteral
    target_kind: TargetKindLiteral = "asset"
    priority: int = 0
    confidence: Decimal | None = None
    rationale: str
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)
    watch_condition: WatchConditionPayload | None = None
    trigger_checklist: list[Any] = Field(default_factory=list)
    max_action: dict[str, Any] = Field(default_factory=dict)
    valid_until: datetime | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    # ROB-274 proposal-state fields. All optional so legacy callers
    # (operation=None) continue to validate unchanged.
    target_ref: TargetRefPayload | None = None
    current_state: dict[str, Any] | None = None
    proposed_state: dict[str, Any] | None = None
    diff: list[dict[str, Any]] | None = None
    apply_policy: ApplyPolicyLiteral | None = None

    # ROB-308 — final-item classification (held action vs new candidate) +
    # per-item source citations. All optional; legacy items omit them.
    decision_bucket: str | None = None
    cited_symbol_report_uuid: UUID | None = None
    cited_dimension_report_uuids: list[UUID] = Field(default_factory=list)
    cited_snapshot_uuids: list[UUID] = Field(default_factory=list)

    @field_validator("decision_bucket")
    @classmethod
    def _decision_bucket_in_vocab(cls, v: str | None) -> str | None:
        from app.models.investment_symbol_intermediate_reports import DECISION_BUCKETS

        if v is not None and v not in DECISION_BUCKETS:
            raise ValueError(f"decision_bucket={v!r} not in {DECISION_BUCKETS!r}")
        return v

    @model_validator(mode="after")
    def _validate_watch_invariants(self) -> IngestReportItem:
        # Legacy callers (operation=None) keep the old invariant.
        if self.item_kind == "watch" and self.operation in (None, "create", "modify"):
            if self.watch_condition is None:
                raise ValueError(
                    "watch items require watch_condition when "
                    "operation is null/'create'/'modify'"
                )
            if self.valid_until is None:
                raise ValueError(
                    "watch items require valid_until when "
                    "operation is null/'create'/'modify'"
                )
        return self

    @model_validator(mode="after")
    def _validate_max_action(self) -> IngestReportItem:
        if (
            self.item_kind == "watch"
            and self.operation in ("create", "modify")
            and self.max_action
        ):
            MaxActionPayload.model_validate(self.max_action)
        return self

    @model_validator(mode="after")
    def _validate_proposal_invariants(self) -> IngestReportItem:
        if self.operation in ("modify",):
            missing: list[str] = []
            if self.target_ref is None:
                missing.append("target_ref")
            if self.current_state is None:
                missing.append("current_state")
            if self.proposed_state is None:
                missing.append("proposed_state")
            if self.diff is None:
                missing.append("diff")
            if missing:
                raise ValueError(f"operation='modify' requires {missing}")
        if self.operation in ("cancel", "keep"):
            missing = []
            if self.target_ref is None:
                missing.append("target_ref")
            if self.current_state is None:
                missing.append("current_state")
            if missing:
                raise ValueError(f"operation={self.operation!r} requires {missing}")
        # operation='review' is intentionally permissive: review can mean
        # (a) ambiguous target with candidates, (b) stale operational state,
        # or (c) unknown state when the upstream snapshot was unavailable.
        # Only the universal `rationale` requirement applies.
        return self


class IngestReportRequest(BaseModel):
    """Idempotent report-bundle ingestion request.

    The repository / ingestion service composes deterministic idempotency
    keys from ``report_type`` + ``market`` + ``market_session`` +
    ``kst_date`` + ``generator_version``.
    """

    report_type: str
    market: MarketLiteral
    market_session: MarketSessionLiteral | None = None
    account_scope: AccountScopeLiteral | None = None
    execution_mode: ExecutionModeLiteral = "advisory_only"
    created_by_profile: str
    title: str
    summary: str
    risk_summary: str | None = None
    thesis_text: str | None = None
    no_action_note: str | None = None
    market_snapshot: dict[str, Any] = Field(default_factory=dict)
    portfolio_snapshot: dict[str, Any] = Field(default_factory=dict)
    previous_report_uuid: UUID | None = None
    status: Literal["draft", "published"] = "draft"
    metadata: dict[str, Any] = Field(default_factory=dict)
    valid_until: datetime | None = None
    published_at: datetime | None = None
    items: list[IngestReportItem] = Field(default_factory=list)

    # Deterministic idempotency components.
    generator_version: str = "v1"
    kst_date: str

    # ROB-269 Phase 3 — bundle linkage + stale-gate inputs. All optional;
    # legacy callers omit them and the DB CHECK's legacy clause
    # (snapshot_freshness_summary IS NULL) lets the row through.
    snapshot_bundle_uuid: UUID | None = None
    snapshot_policy_version: str | None = None
    snapshot_coverage_summary: dict[str, Any] | None = None
    snapshot_freshness_summary: dict[str, Any] | None = None
    """When set, MUST carry an ``overall`` key whose value is one of
    ``fresh|soft_stale|partial|hard_stale|failed|unavailable`` — the DB
    CHECK rejects ``published`` rows whose ``overall`` is not in
    {fresh, soft_stale, partial}."""
    source_conflicts: dict[str, Any] | None = None
    unavailable_sources: dict[str, Any] | None = None
    # ROB-318 Phase 3 (PR-B) — deterministic report diagnostics bundle:
    # {why_no_action, data_sufficiency_by_source, report_quality_summary}.
    snapshot_report_diagnostics: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_advisory_only(self) -> IngestReportRequest:
        # Defense in depth — DB CHECK already enforces this.
        if self.account_scope == "kis_live" and self.execution_mode != "advisory_only":
            raise ValueError(
                "account_scope='kis_live' requires execution_mode='advisory_only'"
            )
        if self.market_session == "nxt" and self.execution_mode != "advisory_only":
            raise ValueError(
                "market_session='nxt' requires execution_mode='advisory_only'"
            )
        return self


class RecordDecisionRequest(BaseModel):
    """Operator decision on a single item.

    ``partial_approve`` requires a non-empty ``approved_payload_snapshot``
    — the snapshot is the canonical record of what was scoped down (e.g.
    ``{"max_notional_krw": 100_000}``). A partial approve without scope
    is indistinguishable from a full approve and should not transition
    the item.
    """

    item_uuid: UUID
    decision: DecisionVerbLiteral
    actor: str
    decision_note: str | None = None
    approved_payload_snapshot: dict[str, Any] | None = None
    idempotency_key: str | None = None

    @model_validator(mode="after")
    def _validate_partial_approve_has_payload(self) -> RecordDecisionRequest:
        if self.decision == "partial_approve" and not self.approved_payload_snapshot:
            raise ValueError(
                "partial_approve requires non-empty approved_payload_snapshot"
            )
        return self


class SetReportStatusRequest(BaseModel):
    """ROB-455 — operator request to transition a report's lifecycle status."""

    report_uuid: UUID
    status: ReportStatusTransitionLiteral
    reason: str | None = None
    actor: str | None = None


class WatchInvalidation(BaseModel):
    """ROB-337 — when a dip-buy watch thesis is invalidated."""

    kind: Literal["price_below", "condition_text"]
    price: Decimal | None = None
    text: str | None = None

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_kind(self) -> WatchInvalidation:
        if self.kind == "price_below" and self.price is None:
            raise ValueError("invalidation kind='price_below' requires price")
        if self.kind == "condition_text" and not self.text:
            raise ValueError("invalidation kind='condition_text' requires text")
        return self


class WatchPriceRange(BaseModel):
    """ROB-337 — suggested limit price band [low, high]."""

    low: Decimal
    high: Decimal

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_order(self) -> WatchPriceRange:
        if self.low > self.high:
            raise ValueError("WatchPriceRange.low must be <= high")
        return self


class WatchRecommendationEvidence(BaseModel):
    """ROB-337 — deterministic evidence behind a watch recommendation."""

    support: Decimal | None = None
    resistance: Decimal | None = None
    spread_bps: Decimal | None = None
    volatility_pct: Decimal | None = None
    lookback_days: int
    news_ref: str | None = None
    screener_reason: str | None = None

    model_config = ConfigDict(extra="forbid")


class WatchRecommendationPayload(BaseModel):
    """ROB-337 Slice 1 — advisory price-review thresholds for a watch item.

    Persisted as JSONB in ``investment_report_items.watch_recommendation``.
    Advisory only — no order is created or submitted from this payload.
    """

    watch_reason: str
    data_state: Literal["ok", "data_gap"]
    reference_price: Decimal | None = None
    entry_review_below_price: Decimal | None = None
    suggested_limit_price_range: WatchPriceRange | None = None
    max_chase_price: Decimal | None = None
    invalidation: WatchInvalidation | None = None
    expiry_at: datetime | None = None
    review_cadence: str = "daily"
    source_evidence: WatchRecommendationEvidence
    policy_version: str
    computed_at: datetime

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _check_ok_completeness(self) -> WatchRecommendationPayload:
        if self.data_state == "ok":
            missing = [
                name
                for name, val in (
                    ("reference_price", self.reference_price),
                    ("entry_review_below_price", self.entry_review_below_price),
                    ("suggested_limit_price_range", self.suggested_limit_price_range),
                    ("max_chase_price", self.max_chase_price),
                    ("invalidation", self.invalidation),
                )
                if val is None
            ]
            if missing:
                raise ValueError(f"data_state='ok' requires {missing}")
        return self


class ActivateWatchRequest(BaseModel):
    """Activate an approved watch item into ``investment_watch_alerts``."""

    item_uuid: UUID
    actor: str
    idempotency_key: str | None = None
    # ROB-393 — operation='review' watches are created without a condition
    # (schema + DB CHECK both exempt them). Allow supplying the condition /
    # expiry at activation time so such a watch can still be armed. Auto
    # derivation of the condition is out of scope (ROB-337 seam).
    watch_condition: WatchConditionPayload | None = None
    valid_until: datetime | None = None


# ---------------------------------------------------------------------------
# Response models (Plan 3 — HTTP / MCP read surface)
# ---------------------------------------------------------------------------
#
# All response models use ``from_attributes=True`` so they can be built
# directly from an ORM instance via ``Model.model_validate(row)``.
# ``populate_by_name=True`` is set on response models that need to surface
# a ``metadata`` JSON field — the ORM attribute is ``report_metadata``
# but the API contract exposes the plain ``metadata`` key.


class InvestmentReportResponse(BaseModel):
    """Single ``investment_reports`` row, serialised for HTTP / MCP."""

    report_uuid: UUID
    report_type: str
    market: MarketLiteral
    market_session: MarketSessionLiteral | None
    account_scope: AccountScopeLiteral | None
    execution_mode: ExecutionModeLiteral
    created_by_profile: str
    title: str
    summary: str
    risk_summary: str | None
    thesis_text: str | None
    no_action_note: str | None
    market_snapshot: dict[str, Any]
    portfolio_snapshot: dict[str, Any]
    previous_report_uuid: UUID | None
    status: ReportStatusLiteral
    metadata: dict[str, Any] = Field(
        validation_alias="report_metadata", serialization_alias="metadata"
    )
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None
    valid_until: datetime | None

    # ROB-269 Phase 3 — snapshot bundle linkage + 3-layer stale gate inputs.
    # All optional so legacy reports (no snapshot metadata) serialise cleanly
    # with explicit ``null`` JSON values. Phase 4 UI renders these directly.
    snapshot_bundle_uuid: UUID | None = None
    snapshot_policy_version: str | None = None
    snapshot_coverage_summary: dict[str, Any] | None = None
    snapshot_freshness_summary: dict[str, Any] | None = None
    source_conflicts: dict[str, Any] | None = None
    unavailable_sources: dict[str, Any] | None = None
    # ROB-318 Phase 3 (PR-B) — {why_no_action, data_sufficiency_by_source,
    # report_quality_summary}. Null on legacy reports.
    snapshot_report_diagnostics: dict[str, Any] | None = None

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class InvestmentReportItemResponse(BaseModel):
    """Single ``investment_report_items`` row."""

    item_uuid: UUID
    item_kind: ItemKindLiteral
    symbol: str | None
    side: ItemSideLiteral | None
    intent: ItemIntentLiteral
    target_kind: TargetKindLiteral
    priority: int
    confidence: Decimal | None
    rationale: str
    evidence_snapshot: dict[str, Any]
    watch_condition: dict[str, Any] | None
    watch_recommendation: dict[str, Any] | None = None
    trigger_checklist: list[Any]
    max_action: dict[str, Any]
    valid_until: datetime | None
    status: ItemStatusLiteral
    metadata: dict[str, Any] = Field(
        validation_alias="item_metadata", serialization_alias="metadata"
    )
    created_at: datetime
    updated_at: datetime

    # ROB-274 — proposal-state fields. Legacy rows have these as NULL.
    operation: OperationLiteral | None = None
    target_ref: dict[str, Any] | None = None
    current_state: dict[str, Any] | None = None
    proposed_state: dict[str, Any] | None = None
    diff: list[dict[str, Any]] | None = None
    apply_policy: ApplyPolicyLiteral | None = None

    # ROB-308 — final-item classification + citations
    decision_bucket: str | None = None
    cited_symbol_report_uuid: UUID | None = None
    cited_dimension_report_uuids: list[UUID] = Field(default_factory=list)
    cited_snapshot_uuids: list[UUID] = Field(default_factory=list)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class InvestmentReportItemDecisionResponse(BaseModel):
    """Single ``investment_report_item_decisions`` row."""

    decision_uuid: UUID
    decision: DecisionVerbLiteral
    actor: str
    decision_note: str | None
    approved_payload_snapshot: dict[str, Any] | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class InvestmentWatchAlertResponse(BaseModel):
    """Single ``investment_watch_alerts`` row."""

    alert_uuid: UUID
    source_report_uuid: UUID
    source_item_uuid: UUID
    market: MarketLiteral
    target_kind: TargetKindLiteral
    symbol: str
    metric: WatchMetricLiteral
    operator: WatchOperatorLiteral
    threshold: Decimal
    threshold_key: str
    intent: ItemIntentLiteral
    action_mode: WatchActionModeLiteral
    rationale: str
    trigger_checklist: list[Any]
    max_action: dict[str, Any]
    valid_until: datetime
    status: Literal["active", "triggered", "expired", "canceled"]
    metadata: dict[str, Any] = Field(
        validation_alias="alert_metadata", serialization_alias="metadata"
    )
    created_at: datetime
    activated_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class InvestmentWatchEventResponse(BaseModel):
    """Single ``investment_watch_events`` row.

    Plan 4 hardening — Hermes delivery tracking columns
    (``delivery_status`` / ``delivery_reason`` / ``delivered_at`` /
    ``delivery_attempts``) are surfaced so operators / frontend can
    see whether the notification actually reached Hermes. The alert
    is only consumed (status='triggered') once delivery_status reaches
    ``delivered``; a ``skipped`` or ``failed`` row means the watch is
    still active and the next scan loop will re-attempt.
    """

    event_uuid: UUID
    alert_id: int | None
    source_report_uuid: UUID
    source_item_uuid: UUID
    market: MarketLiteral
    target_kind: TargetKindLiteral
    symbol: str
    metric: WatchMetricLiteral
    operator: WatchOperatorLiteral
    threshold: Decimal
    threshold_key: str
    intent: ItemIntentLiteral
    action_mode: WatchActionModeLiteral
    current_value: Decimal | None
    scanner_snapshot: dict[str, Any]
    outcome: Literal[
        "notified",
        "review_required",
        "preview_attached",
        "expired",
        "ignored",
        "failed",
    ]
    follow_up_report_item_id: int | None
    correlation_id: str
    kst_date: str
    delivery_status: Literal["pending", "delivered", "skipped", "failed"]
    delivery_reason: str | None
    delivered_at: datetime | None
    delivery_attempts: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


# ROB-322 — KR /invest/reports five-section review surface. These are a
# *view-layer projection* over the locked ROB-301 ``decision_bucket`` vocab +
# ROB-318 report diagnostics; they introduce NO new persisted classification,
# DB CHECK, or ``decision_bucket`` enum value.
ReviewSectionKeyLiteral = Literal[
    "new_buy_candidate",
    "held_strategy_review",
    "watch_only",
    "excluded_or_unavailable",
]
# Mirrors WhyNoActionKind in app/services/action_report/common/diagnostics.py
# (kept local to avoid a schema -> service import, matching this file's pattern
# of mirroring DB/service enums as Literals).
WhyNoActionKindLiteral = Literal["data_insufficient", "stale_gated", "real_no_action"]


class ReviewSection(BaseModel):
    """One ordered review queue (sections 1-4 of ROB-322)."""

    key: ReviewSectionKeyLiteral
    label_ko: str
    items: list[InvestmentReportItemResponse] = Field(default_factory=list)


class NoActionSummary(BaseModel):
    """Section 5 — report-level no-action summary.

    Distinguishes genuine no-action (``real_no_action``) from
    ``stale_gated`` / ``data_insufficient`` no-action, derived from the
    ROB-318 ``snapshot_report_diagnostics.why_no_action`` block. ``kind`` is
    null on legacy reports that lack diagnostics.
    """

    kind: WhyNoActionKindLiteral | None = None
    reason_ko: str | None = None
    blocking_sources: list[str] = Field(default_factory=list)
    excluded_count: int = 0


class ReportReviewSections(BaseModel):
    """ROB-322 five-section actionable review projection.

    ``sections`` always carries the four queues in fixed display order (each
    may be empty); ``no_action_summary`` is null when there is nothing to
    summarise. Legacy items with ``decision_bucket=None`` are intentionally
    not projected here and remain available via ``items`` / ``item_groups``.
    """

    sections: list[ReviewSection] = Field(default_factory=list)
    no_action_summary: NoActionSummary | None = None


# ROB-335 — intraday ActionPacket. A read-time *view-layer projection* over
# the same persisted items (sub-verdict in evidence_snapshot["action_verdict"])
# + ROB-318 diagnostics. No new persisted classification / DB CHECK / migration.
ActionVerdictLiteral = Literal[
    "buy_review",
    "limit_wait",
    "no_new_buy_candidates",
    "sell_review",
    "trim_review",
    "add_review",
    "keep",
    "no_add",
    "watch_only",
    "rejected",
    "data_gap",
]


class ActionPacketEntry(BaseModel):
    """One symbol-level entry in an ActionPacket group."""

    verdict: ActionVerdictLiteral
    symbol: str | None = None
    side: ItemSideLiteral | None = None
    rationale: str
    item_uuid: UUID | None = None
    priority: int | None = None
    rank: int | None = None
    reject_or_wait_reason: str | None = None
    evidence_snapshot: dict[str, Any] = Field(default_factory=dict)


class DataGapEntry(BaseModel):
    """One data-gap surfaced for the next cycle."""

    source: str
    status: str | None = None
    reason: str | None = None


class ActionPacket(BaseModel):
    """ROB-335 four-question intraday surface (held / new / risk / data-gap).

    Always-explicit: ``no_new_buy_reason`` and ``no_action_reason`` answer the
    "why nothing" questions even when the corresponding groups are empty.
    """

    held_actions: list[ActionPacketEntry] = Field(default_factory=list)
    new_buy_candidates: list[ActionPacketEntry] = Field(default_factory=list)
    no_new_buy_reason: str | None = None
    risk_reviews: list[ActionPacketEntry] = Field(default_factory=list)
    no_action_reason: NoActionSummary | None = None
    data_gaps_for_next_cycle: list[DataGapEntry] = Field(default_factory=list)


class InvestmentReportNewsCitationResponse(BaseModel):
    """ROB-423 — one cited news article on a report (read-side)."""

    citation_uuid: UUID
    report_item_uuid: UUID | None = None
    section_key: str | None = None
    market: str
    symbol: str
    provider: str
    external_article_id: str | None = None
    canonical_url: str
    source_name: str | None = None
    title: str
    summary_snapshot: str | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    relevance: str
    role: str
    decision_impact: str
    selection_reason: str | None = None
    confidence: Decimal | None = None

    model_config = ConfigDict(from_attributes=True)


class InvestmentReportBundle(BaseModel):
    """``investment_report_get`` / ``GET /.../investment-reports/{uuid}``.

    ``decisions_by_item_uuid`` is keyed by string UUID for external
    consumers (the service-layer dict is keyed by the integer item.id).
    """

    report: InvestmentReportResponse
    items: list[InvestmentReportItemResponse]
    decisions_by_item_uuid: dict[str, list[InvestmentReportItemDecisionResponse]]
    alerts: list[InvestmentWatchAlertResponse]
    events: list[InvestmentWatchEventResponse]
    # ROB-308 — read-side grouping
    item_groups: dict[str, list[InvestmentReportItemResponse]] = Field(
        default_factory=dict
    )
    decision_rollup: dict[str, list[InvestmentReportItemResponse]] = Field(
        default_factory=dict
    )
    # ROB-322 — additive five-section review projection. Null/empty for legacy
    # reports; existing ``items`` / ``item_groups`` remain the fallback.
    review_sections: ReportReviewSections | None = None
    # ROB-335 — additive intraday ActionPacket projection. Null for legacy /
    # non-intraday reports; existing items / review_sections remain the fallback.
    action_packet: ActionPacket | None = None
    # ROB-423 — additive news citations (articles the report actually used).
    # Empty for reports with no Hermes-marked news.
    news_citations: list[InvestmentReportNewsCitationResponse] = Field(
        default_factory=list
    )


class InvestmentReportListResponse(BaseModel):
    """``investment_report_list`` / ``GET /.../investment-reports``."""

    reports: list[InvestmentReportResponse]


class PreviousReportContextResponse(BaseModel):
    """``investment_report_context_get`` / ``GET /.../investment-reports/context``."""

    prior_reports: list[InvestmentReportResponse]
    unresolved_deferred_items: list[InvestmentReportItemResponse]
    active_watches: list[InvestmentWatchAlertResponse]
    triggered_events: list[InvestmentWatchEventResponse]
    recent_decisions: list[InvestmentReportItemDecisionResponse]
    # ROB-274 — pending broker order snapshot for the same market/account.
    # ``None`` means the snapshot was not available at context fetch time
    # (collector unavailable / stale / unsupported scope); an empty list
    # means the broker reported no pending orders.
    pending_orders: list[dict[str, Any]] | None = None


class InvestmentReportCreateResponse(BaseModel):
    """``investment_report_create`` MCP return shape."""

    success: bool = True
    idempotent: bool
    report: InvestmentReportResponse


class InvestmentReportDecideItemResponse(BaseModel):
    """``investment_report_decide_item`` MCP return shape."""

    success: bool = True
    decision: InvestmentReportItemDecisionResponse
    item: InvestmentReportItemResponse


class InvestmentReportActivateWatchResponse(BaseModel):
    """``investment_report_activate_watch`` MCP return shape."""

    success: bool = True
    alert: InvestmentWatchAlertResponse
    item: InvestmentReportItemResponse


# ---------------------------------------------------------------------------
# ROB-275 — Report-centric snapshot evidence viewer response shapes.
#
# These wrap the existing investment_snapshots read shapes for use under
# the /invest/api/investment-reports/{report_uuid}/snapshot-bundle and
# .../snapshots/{snapshot_uuid} endpoints. The /trading/api/investment-snapshots
# MCP-flag-gated routes are NOT touched.
# ---------------------------------------------------------------------------


class ReportSnapshotBundleSummaryView(BaseModel):
    """Bundle header surfaced via the report-centric evidence endpoint."""

    bundle_uuid: UUID
    purpose: str
    market: SnapshotMarket
    account_scope: SnapshotAccountScope | None
    policy_version: str
    status: BundleStatus
    as_of: datetime
    coverage_summary: dict[str, Any]
    freshness_summary: dict[str, Any]
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ReportSnapshotBundleItemView(BaseModel):
    """One row in the report's snapshot evidence list — metadata only.

    ``payload_size_bytes`` is computed from the snapshot's stored JSON in
    the service layer; clients use it to hint at how heavy a payload
    fetch will be without actually downloading it.
    """

    snapshot_uuid: UUID
    role: BundleItemRole
    snapshot_kind: SnapshotKind
    source_kind: SourceKind
    market: SnapshotMarket
    symbol: str | None
    account_scope: SnapshotAccountScope | None
    freshness_status: FreshnessStatus
    as_of: datetime
    valid_until: datetime | None
    source_table: str | None
    source_id: int | None
    source_uri: str | None
    payload_size_bytes: int | None

    model_config = ConfigDict(from_attributes=True)


class ReportSnapshotBundleResponse(BaseModel):
    """``GET /invest/api/investment-reports/{report_uuid}/snapshot-bundle``.

    ``legacy_no_snapshot=True`` means the report exists but has no
    ``snapshot_bundle_uuid`` — caller renders a legacy message.
    """

    bundle: ReportSnapshotBundleSummaryView | None = None
    items: list[ReportSnapshotBundleItemView] = Field(default_factory=list)
    unavailable_sources: dict[str, Any] | None = None
    source_conflicts: dict[str, Any] | None = None
    legacy_no_snapshot: bool = False


class ReportSnapshotDetailResponse(BaseModel):
    """``GET /invest/api/investment-reports/{report_uuid}/snapshots/{snapshot_uuid}``.

    Returned only after a successful membership check
    (snapshot_uuid ∈ this report's bundle_items). Carries the snapshot's
    full DB payload + metadata, plus the bundle item's role/context.
    """

    snapshot_uuid: UUID
    role: BundleItemRole
    snapshot_kind: SnapshotKind
    source_kind: SourceKind
    market: SnapshotMarket
    symbol: str | None
    account_scope: SnapshotAccountScope | None
    source_table: str | None
    source_id: int | None
    source_uri: str | None
    freshness_status: FreshnessStatus
    as_of: datetime
    valid_until: datetime | None
    source_timestamps_json: dict[str, Any]
    coverage_json: dict[str, Any]
    errors_json: dict[str, Any]
    payload_json: dict[str, Any]

    model_config = ConfigDict(from_attributes=True)
