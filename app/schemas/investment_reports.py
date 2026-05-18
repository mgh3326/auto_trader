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

from pydantic import BaseModel, ConfigDict, Field, model_validator

# Enum-equivalent Literals — match the DB CHECK constraints from
# alembic/versions/20260518_rob265_add_investment_reports.py exactly.
MarketLiteral = Literal["kr", "us", "crypto"]
MarketSessionLiteral = Literal["regular", "nxt", "pre", "post", "24x7"]
AccountScopeLiteral = Literal["kis_live", "kis_mock", "alpaca_paper", "upbit_live"]
ExecutionModeLiteral = Literal["advisory_only", "mock_preview"]
ReportStatusLiteral = Literal["draft", "published", "decided", "expired", "superseded"]

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
WatchActionModeLiteral = Literal["notify_only", "preview_only", "approval_required"]

DecisionVerbLiteral = Literal["approve", "deny", "defer", "skip", "partial_approve"]


class WatchConditionPayload(BaseModel):
    """Embedded condition for a watch item. Persisted as JSONB."""

    metric: WatchMetricLiteral
    operator: WatchOperatorLiteral
    threshold: Decimal
    threshold_key: str | None = None
    target_kind: TargetKindLiteral = "asset"
    action_mode: WatchActionModeLiteral = "notify_only"

    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _default_threshold_key(self) -> WatchConditionPayload:
        # Canonical form: str(Decimal). Caller can override if they need
        # a different dedup key (e.g. rounded value).
        if self.threshold_key is None:
            self.threshold_key = str(self.threshold)
        return self


class IngestReportItem(BaseModel):
    """One proposal item attached to an ingested report."""

    item_kind: ItemKindLiteral
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

    @model_validator(mode="after")
    def _validate_watch_invariants(self) -> IngestReportItem:
        if self.item_kind == "watch":
            if self.watch_condition is None:
                raise ValueError(
                    "watch items must carry watch_condition (item_kind='watch')"
                )
            if self.valid_until is None:
                raise ValueError(
                    "watch items must carry valid_until (item_kind='watch')"
                )
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
    """Operator decision on a single item."""

    item_uuid: UUID
    decision: DecisionVerbLiteral
    actor: str
    decision_note: str | None = None
    approved_payload_snapshot: dict[str, Any] | None = None
    idempotency_key: str | None = None


class ActivateWatchRequest(BaseModel):
    """Activate an approved watch item into ``investment_watch_alerts``."""

    item_uuid: UUID
    actor: str
    idempotency_key: str | None = None
