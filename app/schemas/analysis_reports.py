"""Schemas for ROB-257 analyst report action-center artifacts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ReportStatus = Literal["draft", "published", "superseded", "expired"]
StageStatus = Literal["ok", "stale", "unavailable", "error"]
CandidateSide = Literal["buy", "sell"]
ApprovalStatus = Literal["awaiting_approval", "approved", "rejected", "expired"]
ExecutionState = Literal["not_submitted", "blocked", "submitted_elsewhere"]


class AnalysisStageResultCreate(BaseModel):
    stage_key: str
    source: str
    provenance: dict[str, Any] = Field(default_factory=dict)
    status: StageStatus
    freshness_at: datetime | None = None
    raw_payload: dict[str, Any] | None = None
    normalized_payload: dict[str, Any] = Field(default_factory=dict)
    unavailable_reason: str | None = None
    warnings: list[str] = Field(default_factory=list)


class AnalysisOrderCandidateCreate(BaseModel):
    idempotency_key: str
    symbol: str
    market: str
    side: CandidateSide
    action_type: str
    quantity: Decimal | None = None
    quantity_pct: Decimal | None = None
    limit_price: Decimal | None = None
    notional: Decimal | None = None
    currency: str | None = None
    priority: int = 0
    confidence: Decimal | None = None
    thesis: str
    risk_notes: list[str] = Field(default_factory=list)
    verification: dict[str, Any] = Field(default_factory=dict)
    blocking_reasons: list[str] = Field(default_factory=list)
    approval_status: ApprovalStatus = "awaiting_approval"
    approval_type: str = "manual"
    policy_id: str | None = None
    policy_snapshot: dict[str, Any] | None = None
    execution_state: ExecutionState = "not_submitted"
    valid_until: datetime | None = None

    @field_validator("execution_state")
    @classmethod
    def _no_submitted_execution_state(cls, value: ExecutionState) -> ExecutionState:
        if value != "not_submitted":
            raise ValueError(
                "analysis candidates are decision artifacts and must not be submitted"
            )
        return value


class AnalysisReportCreateRequest(BaseModel):
    idempotency_key: str
    report_type: str
    market: str
    account_scope: str | None = None
    status: ReportStatus = "draft"
    summary: str
    risk_summary: str | None = None
    data_freshness: dict[str, Any] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    source_policy: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    stage_results: list[AnalysisStageResultCreate] = Field(default_factory=list)
    candidates: list[AnalysisOrderCandidateCreate] = Field(default_factory=list)
    published_at: datetime | None = None
    valid_until: datetime | None = None


class AnalysisStageResultResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    stage_key: str
    source: str
    provenance: dict[str, Any]
    status: StageStatus
    freshness_at: datetime | None
    raw_payload: dict[str, Any] | None
    normalized_payload: dict[str, Any]
    unavailable_reason: str | None
    warnings: list[str]
    created_at: datetime


class AnalysisOrderCandidateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    candidate_uuid: str
    report_uuid: str | None = None
    idempotency_key: str
    symbol: str
    market: str
    side: CandidateSide
    action_type: str
    quantity: Decimal | None
    quantity_pct: Decimal | None
    limit_price: Decimal | None
    notional: Decimal | None
    currency: str | None
    priority: int
    confidence: Decimal | None
    thesis: str
    risk_notes: list[str]
    verification: dict[str, Any]
    blocking_reasons: list[str]
    approval_status: ApprovalStatus
    approval_type: str
    approved_by: str | None = None
    approved_at: datetime | None = None
    rejected_by: str | None = None
    rejected_at: datetime | None = None
    policy_id: str | None = None
    policy_snapshot: dict[str, Any] | None = None
    execution_state: ExecutionState
    linked_trade_journal_id: int | None = None
    linked_order_ledger_ref: str | None = None
    created_at: datetime
    valid_until: datetime | None = None


class AnalysisReportResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    report_uuid: str
    idempotency_key: str
    report_type: str
    market: str
    account_scope: str | None
    created_by_profile: str
    status: ReportStatus
    summary: str
    risk_summary: str | None
    data_freshness: dict[str, Any]
    coverage: dict[str, Any]
    source_policy: list[str]
    safety_notes: list[str]
    metadata: dict[str, Any]
    created_at: datetime
    published_at: datetime | None = None
    valid_until: datetime | None = None
    stages: list[AnalysisStageResultResponse] = Field(default_factory=list)
    candidates: list[AnalysisOrderCandidateResponse] = Field(default_factory=list)
    idempotent: bool = False


class AnalysisReportListResponse(BaseModel):
    count: int
    items: list[AnalysisReportResponse]


class AnalysisCandidateListResponse(BaseModel):
    count: int
    items: list[AnalysisOrderCandidateResponse]
