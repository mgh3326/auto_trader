"""Trading decisions API schemas."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

# Shared type literals per plan §6
ProposalKindLiteral = Literal[
    "trim",
    "add",
    "enter",
    "exit",
    "pullback_watch",
    "breakout_watch",
    "avoid",
    "no_action",
    "other",
]

SideLiteral = Literal["buy", "sell", "none"]

UserResponseLiteral = Literal[
    "pending",
    "accept",
    "reject",
    "modify",
    "partial_accept",
    "defer",
]

ActionKindLiteral = Literal[
    "live_order",
    "paper_order",
    "watch_alert",
    "no_action",
    "manual_note",
]

TrackKindLiteral = Literal[
    "accepted_live",
    "accepted_paper",
    "rejected_counterfactual",
    "analyst_alternative",
    "user_alternative",
]

OutcomeHorizonLiteral = Literal["1h", "4h", "1d", "3d", "7d", "final"]

SessionStatusLiteral = Literal["open", "closed", "archived"]

WorkflowStatusLiteral = Literal[
    "created",
    "evidence_generating",
    "evidence_ready",
    "debate_ready",
    "trader_draft_ready",
    "risk_review_ready",
    "auto_approved",
    "preview_ready",
    "journal_ready",
    "completed",
    "failed_evidence",
    "failed_trader_draft",
    "failed_risk_review",
    "preview_blocked",
]

AccountModeLiteral = Literal["kis_mock", "alpaca_paper", "kis_live", "db_simulated"]

InstrumentTypeLiteral = Literal[
    "equity_kr",
    "equity_us",
    "crypto",
    "forex",
    "index",
]


# ========== Committee Sub-Schemas ==========


class CommitteeAnalysisSub(BaseModel):
    summary: str | None = None
    confidence: int | None = None
    payload: dict | None = None


class CommitteeEvidence(BaseModel):
    technical_analysis: CommitteeAnalysisSub | None = None
    news_analysis: CommitteeAnalysisSub | None = None
    on_chain_analysis: CommitteeAnalysisSub | None = None


class CommitteeDebateClaim(BaseModel):
    text: str
    weight: Literal["low", "medium", "high"] = "medium"
    source: Literal["technical", "news", "portfolio", "fundamentals", "sentiment"] = (
        "technical"
    )


class CommitteeResearchDebate(BaseModel):
    bull_case: list[CommitteeDebateClaim] = Field(default_factory=list)
    bear_case: list[CommitteeDebateClaim] = Field(default_factory=list)
    summary: str | None = None


CommitteeTraderActionLiteral = Literal[
    "BUY", "HOLD", "TRIM", "SELL", "AVOID", "WATCH", "REBALANCE"
]


class CommitteeTraderDraft(BaseModel):
    symbol: str
    action: CommitteeTraderActionLiteral
    price_plan: str | None = None
    size_plan: str | None = None
    rationale: str | None = None
    confidence: Literal["low", "medium", "high"] = "medium"
    invalidation_condition: str | None = None
    next_step_recommendation: str | None = None
    is_live_order: Literal[False] = False


class CommitteeRiskReview(BaseModel):
    verdict: Literal["approved", "vetoed", "flagged"]
    notes: str | None = None
    reviewed_at: datetime | None = None


class CommitteePortfolioApproval(BaseModel):
    verdict: Literal["approved", "vetoed", "modified"]
    notes: str | None = None
    approved_at: datetime | None = None


class CommitteeExecutionPreview(BaseModel):
    is_blocked: bool = False
    block_reason: str | None = None
    preview_payload: dict | None = None


class CommitteeJournalPlaceholder(BaseModel):
    journal_uuid: UUID | None = None
    notes: str | None = None


COMMITTEE_SOURCE_PROFILE = "committee_mock_paper"
COMMITTEE_ALLOWED_ACCOUNT_MODES: frozenset[str] = frozenset(
    {"kis_mock", "alpaca_paper"}
)


class CommitteeAutomation(BaseModel):
    enabled: bool = False
    auto_approve_risk: bool = False
    auto_execute: bool = False

    @model_validator(mode="after")
    def _enforce_no_live_auto_execute(self) -> Self:
        # ROB-107 safety: committee MVP must never auto-execute live orders.
        # auto_execute=True would mean a live broker submit on auto-approval,
        # which is explicitly out of scope for KIS mock / Alpaca paper sessions.
        if self.auto_execute:
            raise ValueError(
                "auto_execute must be False for committee MVP "
                "(live execution is disabled)"
            )
        return self


class CommitteeArtifacts(BaseModel):
    evidence: CommitteeEvidence | None = None
    research_debate: CommitteeResearchDebate | None = None
    trader_draft: list[CommitteeTraderDraft] | None = None
    risk_review: CommitteeRiskReview | None = None
    portfolio_approval: CommitteePortfolioApproval | None = None
    execution_preview: CommitteeExecutionPreview | None = None
    journal_placeholder: CommitteeJournalPlaceholder | None = None


# ========== Session Schemas ==========


class SessionCreateRequest(BaseModel):
    source_profile: str = Field(..., min_length=1, max_length=64)
    strategy_name: str | None = Field(default=None, max_length=128)
    market_scope: str | None = Field(default=None, max_length=32)
    market_brief: dict | None = None
    generated_at: datetime
    notes: str | None = Field(default=None, max_length=4000)
    workflow_status: WorkflowStatusLiteral | None = None
    account_mode: AccountModeLiteral | None = None
    automation: CommitteeAutomation | None = None

    @model_validator(mode="after")
    def _enforce_committee_simulation_only(self) -> Self:
        # ROB-107 safety: a committee_mock_paper session must restrict
        # account_mode to simulation broker accounts. kis_live and db_simulated
        # are rejected at the contract layer so the live trading code paths
        # cannot be reached even by a malformed client.
        if self.source_profile == COMMITTEE_SOURCE_PROFILE:
            if self.account_mode is None:
                raise ValueError(
                    "committee sessions require account_mode "
                    "(one of: kis_mock, alpaca_paper)"
                )
            if self.account_mode not in COMMITTEE_ALLOWED_ACCOUNT_MODES:
                raise ValueError(
                    f"committee sessions reject account_mode={self.account_mode!r}; "
                    f"only kis_mock and alpaca_paper are allowed"
                )
        return self


class SessionSummary(BaseModel):
    session_uuid: UUID
    source_profile: str
    strategy_name: str | None
    market_scope: str | None
    status: SessionStatusLiteral
    workflow_status: WorkflowStatusLiteral | None = None
    account_mode: AccountModeLiteral | None = None
    generated_at: datetime
    created_at: datetime
    updated_at: datetime
    proposals_count: int
    pending_count: int


class SessionDetail(SessionSummary):
    market_brief: dict | None
    notes: str | None
    automation: CommitteeAutomation | None = None
    artifacts: CommitteeArtifacts | None = None
    proposals: list["ProposalDetail"]


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]
    total: int
    limit: int
    offset: int


# ========== Proposal Schemas ==========


class ProposalCreateItem(BaseModel):
    symbol: str = Field(..., min_length=1, max_length=64)
    instrument_type: InstrumentTypeLiteral
    proposal_kind: ProposalKindLiteral
    side: SideLiteral = "none"
    original_quantity: Decimal | None = None
    original_quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    original_amount: Decimal | None = Field(default=None, ge=0)
    original_price: Decimal | None = Field(default=None, ge=0)
    original_trigger_price: Decimal | None = Field(default=None, ge=0)
    original_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    original_currency: str | None = Field(default=None, max_length=8)
    original_rationale: str | None = Field(default=None, max_length=4000)
    original_payload: dict


class ProposalCreateBulkRequest(BaseModel):
    proposals: list[ProposalCreateItem] = Field(..., min_length=1, max_length=100)


class ProposalSummary(BaseModel):
    proposal_uuid: UUID
    symbol: str
    instrument_type: InstrumentTypeLiteral
    proposal_kind: ProposalKindLiteral
    side: SideLiteral
    user_response: UserResponseLiteral
    responded_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ProposalDetail(ProposalSummary):
    original_quantity: Decimal | None
    original_quantity_pct: Decimal | None
    original_amount: Decimal | None
    original_price: Decimal | None
    original_trigger_price: Decimal | None
    original_threshold_pct: Decimal | None
    original_currency: str | None
    original_rationale: str | None
    original_payload: dict
    user_quantity: Decimal | None
    user_quantity_pct: Decimal | None
    user_amount: Decimal | None
    user_price: Decimal | None
    user_trigger_price: Decimal | None
    user_threshold_pct: Decimal | None
    user_note: str | None
    actions: list["ActionDetail"]
    counterfactuals: list["CounterfactualDetail"]
    outcomes: list["OutcomeDetail"]


class ProposalCreateBulkResponse(BaseModel):
    proposals: list[ProposalDetail]


# ========== Response Schemas ==========


class ProposalRespondRequest(BaseModel):
    response: Literal["accept", "reject", "modify", "partial_accept", "defer"]
    user_quantity: Decimal | None = None
    user_quantity_pct: Decimal | None = Field(default=None, ge=0, le=100)
    user_amount: Decimal | None = Field(default=None, ge=0)
    user_price: Decimal | None = Field(default=None, ge=0)
    user_trigger_price: Decimal | None = Field(default=None, ge=0)
    user_threshold_pct: Decimal | None = Field(default=None, ge=0, le=100)
    user_note: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def _modify_requires_some_user_field(self) -> Self:
        if self.response in ("modify", "partial_accept") and not any(
            v is not None
            for v in (
                self.user_quantity,
                self.user_quantity_pct,
                self.user_amount,
                self.user_price,
                self.user_trigger_price,
                self.user_threshold_pct,
            )
        ):
            raise ValueError(
                "modify/partial_accept requires at least one user_* numeric field"
            )
        return self


# ========== Action Schemas ==========


class ActionCreateRequest(BaseModel):
    action_kind: ActionKindLiteral
    external_order_id: str | None = Field(default=None, max_length=128)
    external_paper_id: str | None = Field(default=None, max_length=128)
    external_watch_id: str | None = Field(default=None, max_length=128)
    external_source: str | None = Field(default=None, max_length=64)
    payload_snapshot: dict

    @model_validator(mode="after")
    def _kinds_requiring_external_id(self) -> Self:
        needs_id = self.action_kind not in ("no_action", "manual_note")
        has_id = any(
            [self.external_order_id, self.external_paper_id, self.external_watch_id]
        )
        if needs_id and not has_id:
            raise ValueError(
                f"action_kind '{self.action_kind}' requires at least one external_* id"
            )
        return self


class ActionDetail(BaseModel):
    id: int
    action_kind: ActionKindLiteral
    external_order_id: str | None
    external_paper_id: str | None
    external_watch_id: str | None
    external_source: str | None
    payload_snapshot: dict
    recorded_at: datetime
    created_at: datetime


# ========== Counterfactual Schemas ==========


class CounterfactualCreateRequest(BaseModel):
    track_kind: Literal[
        "rejected_counterfactual",
        "analyst_alternative",
        "user_alternative",
        "accepted_paper",
    ]
    baseline_price: Decimal = Field(..., ge=0)
    baseline_at: datetime
    quantity: Decimal | None = None
    payload: dict
    notes: str | None = Field(default=None, max_length=4000)


class CounterfactualDetail(BaseModel):
    id: int
    track_kind: TrackKindLiteral
    baseline_price: Decimal
    baseline_at: datetime
    quantity: Decimal | None
    payload: dict
    notes: str | None
    created_at: datetime


# ========== Outcome Schemas ==========


class OutcomeCreateRequest(BaseModel):
    track_kind: TrackKindLiteral
    horizon: OutcomeHorizonLiteral
    price_at_mark: Decimal = Field(..., ge=0)
    counterfactual_id: int | None = None
    pnl_pct: Decimal | None = None
    pnl_amount: Decimal | None = None
    marked_at: datetime
    payload: dict | None = None

    @model_validator(mode="after")
    def _accepted_live_invariant(self) -> Self:
        if self.track_kind == "accepted_live" and self.counterfactual_id is not None:
            raise ValueError("accepted_live track must not include counterfactual_id")
        if self.track_kind != "accepted_live" and self.counterfactual_id is None:
            raise ValueError(
                f"track_kind '{self.track_kind}' requires counterfactual_id"
            )
        return self


class OutcomeDetail(BaseModel):
    id: int
    counterfactual_id: int | None
    track_kind: TrackKindLiteral
    horizon: OutcomeHorizonLiteral
    price_at_mark: Decimal
    pnl_pct: Decimal | None
    pnl_amount: Decimal | None
    marked_at: datetime
    payload: dict | None
    created_at: datetime


# ========== Analytics Schemas ==========


class SessionAnalyticsCell(BaseModel):
    track_kind: TrackKindLiteral
    horizon: OutcomeHorizonLiteral
    outcome_count: int
    proposal_count: int
    mean_pnl_pct: Decimal | None = None
    sum_pnl_amount: Decimal | None = None
    latest_marked_at: datetime | None = None


class SessionAnalyticsResponse(BaseModel):
    session_uuid: UUID
    generated_at: datetime
    tracks: list[TrackKindLiteral]
    horizons: list[OutcomeHorizonLiteral]
    cells: list[SessionAnalyticsCell]
