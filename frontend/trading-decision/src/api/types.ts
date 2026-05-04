export type Uuid = string;
export type IsoDateTime = string;
export type DecimalString = string;

export type SessionStatus = "open" | "closed" | "archived";

export type WorkflowStatus =
  | "created"
  | "evidence_generating"
  | "evidence_ready"
  | "debate_ready"
  | "trader_draft_ready"
  | "risk_review_ready"
  | "auto_approved"
  | "preview_ready"
  | "journal_ready"
  | "completed"
  | "failed_evidence"
  | "failed_trader_draft"
  | "failed_risk_review"
  | "preview_blocked";

export type CommitteeAccountMode =
  | "kis_mock"
  | "alpaca_paper"
  | "kis_live"
  | "db_simulated";

export type ProposalKind =
  | "trim"
  | "add"
  | "enter"
  | "exit"
  | "pullback_watch"
  | "breakout_watch"
  | "avoid"
  | "no_action"
  | "other";
export type Side = "buy" | "sell" | "none";
export type UserResponseValue =
  | "pending"
  | "accept"
  | "reject"
  | "modify"
  | "partial_accept"
  | "defer";
export type ActionKind =
  | "live_order"
  | "paper_order"
  | "watch_alert"
  | "no_action"
  | "manual_note";
export type TrackKind =
  | "accepted_live"
  | "accepted_paper"
  | "rejected_counterfactual"
  | "analyst_alternative"
  | "user_alternative";
export type OutcomeHorizon = "1h" | "4h" | "1d" | "3d" | "7d" | "final";
export type InstrumentType =
  | "equity_kr"
  | "equity_us"
  | "crypto"
  | "forex"
  | "index";

export interface SessionSummary {
  session_uuid: Uuid;
  source_profile: string;
  strategy_name: string | null;
  market_scope: string | null;
  status: SessionStatus;
  workflow_status?: WorkflowStatus | null;
  account_mode?: CommitteeAccountMode | null;
  generated_at: IsoDateTime;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  proposals_count: number;
  pending_count: number;
}

export interface SessionListResponse {
  sessions: SessionSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface ActionDetail {
  id: number;
  action_kind: ActionKind;
  external_order_id: string | null;
  external_paper_id: string | null;
  external_watch_id: string | null;
  external_source: string | null;
  payload_snapshot: Record<string, unknown>;
  recorded_at: IsoDateTime;
  created_at: IsoDateTime;
}

export interface CounterfactualDetail {
  id: number;
  track_kind: TrackKind;
  baseline_price: DecimalString;
  baseline_at: IsoDateTime;
  quantity: DecimalString | null;
  payload: Record<string, unknown>;
  notes: string | null;
  created_at: IsoDateTime;
}

export interface OutcomeDetail {
  id: number;
  counterfactual_id: number | null;
  track_kind: TrackKind;
  horizon: OutcomeHorizon;
  price_at_mark: DecimalString;
  pnl_pct: DecimalString | null;
  pnl_amount: DecimalString | null;
  marked_at: IsoDateTime;
  payload: Record<string, unknown> | null;
  created_at: IsoDateTime;
}

export interface OutcomeCreateRequest {
  track_kind: TrackKind;
  horizon: OutcomeHorizon;
  price_at_mark: DecimalString;
  counterfactual_id?: number | null;
  pnl_pct?: DecimalString | null;
  pnl_amount?: DecimalString | null;
  marked_at: IsoDateTime;
  payload?: Record<string, unknown> | null;
}

export interface SessionAnalyticsCell {
  track_kind: TrackKind;
  horizon: OutcomeHorizon;
  outcome_count: number;
  proposal_count: number;
  mean_pnl_pct: DecimalString | null;
  sum_pnl_amount: DecimalString | null;
  latest_marked_at: IsoDateTime | null;
}

export interface SessionAnalyticsResponse {
  session_uuid: Uuid;
  generated_at: IsoDateTime;
  tracks: TrackKind[];
  horizons: OutcomeHorizon[];
  cells: SessionAnalyticsCell[];
}

export interface CryptoPaperPreviewPayload {
  symbol: string;
  side: "buy";
  type: "limit";
  notional: DecimalString;
  limit_price: DecimalString;
  time_in_force: "gtc" | "ioc";
  asset_class: "crypto";
}

export interface CryptoPaperWorkflowMetadata {
  signal_symbol: string;
  signal_venue: "upbit";
  execution_symbol: string;
  execution_venue: "alpaca_paper";
  execution_mode: "paper";
  execution_asset_class?: "crypto";
  asset_class?: "crypto";
  workflow_stage?: "crypto_weekend" | "crypto_always_open";
  stage?: "crypto_weekend" | "crypto_always_open";
  purpose: "paper_plumbing_smoke" | "alpha_candidate_review" | string;
  preview_payload: CryptoPaperPreviewPayload;
  approval_copy: string[];
}

export interface ProposalOriginalPayload extends Record<string, unknown> {
  crypto_paper_workflow?: CryptoPaperWorkflowMetadata;
}

export interface ProposalDetail {
  proposal_uuid: Uuid;
  symbol: string;
  instrument_type: InstrumentType;
  proposal_kind: ProposalKind;
  side: Side;
  user_response: UserResponseValue;
  responded_at: IsoDateTime | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime;
  original_quantity: DecimalString | null;
  original_quantity_pct: DecimalString | null;
  original_amount: DecimalString | null;
  original_price: DecimalString | null;
  original_trigger_price: DecimalString | null;
  original_threshold_pct: DecimalString | null;
  original_currency: string | null;
  original_rationale: string | null;
  original_payload: ProposalOriginalPayload;
  user_quantity: DecimalString | null;
  user_quantity_pct: DecimalString | null;
  user_amount: DecimalString | null;
  user_price: DecimalString | null;
  user_trigger_price: DecimalString | null;
  user_threshold_pct: DecimalString | null;
  user_note: string | null;
  actions: ActionDetail[];
  counterfactuals: CounterfactualDetail[];
  outcomes: OutcomeDetail[];
}

export interface SessionDetail extends SessionSummary {
  market_brief: Record<string, unknown> | null;
  notes: string | null;
  automation?: CommitteeAutomation | null;
  artifacts?: CommitteeArtifacts | null;
  proposals: ProposalDetail[];
}

export interface CommitteeAnalysisSub {
  summary: string | null;
  confidence: number | null;
  payload: Record<string, unknown> | null;
}

export interface CommitteeEvidence {
  technical_analysis: CommitteeAnalysisSub | null;
  news_analysis: CommitteeAnalysisSub | null;
  on_chain_analysis: CommitteeAnalysisSub | null;
}

export interface CommitteeRiskReview {
  verdict: "approved" | "vetoed" | "flagged";
  notes: string | null;
  reviewed_at: IsoDateTime | null;
}

export interface CommitteePortfolioApproval {
  verdict: "approved" | "vetoed" | "modified";
  notes: string | null;
  approved_at: IsoDateTime | null;
}

export interface CommitteeExecutionPreview {
  is_blocked: boolean;
  block_reason: string | null;
  preview_payload: Record<string, unknown> | null;
}

export interface CommitteeJournalPlaceholder {
  journal_uuid: Uuid | null;
  notes: string | null;
}

export interface CommitteeAutomation {
  enabled: boolean;
  auto_approve_risk: boolean;
  auto_execute: boolean;
}

export interface CommitteeArtifacts {
  evidence: CommitteeEvidence | null;
  risk_review: CommitteeRiskReview | null;
  portfolio_approval: CommitteePortfolioApproval | null;
  execution_preview: CommitteeExecutionPreview | null;
  journal_placeholder: CommitteeJournalPlaceholder | null;
}

export type RespondAction = Exclude<UserResponseValue, "pending">;

export interface ProposalRespondRequest {
  response: RespondAction;
  user_quantity?: DecimalString | null;
  user_quantity_pct?: DecimalString | null;
  user_amount?: DecimalString | null;
  user_price?: DecimalString | null;
  user_trigger_price?: DecimalString | null;
  user_threshold_pct?: DecimalString | null;
  user_note?: string | null;
}

// Preopen dashboard types (ROB-39)
export interface PreopenCandidateSummary {
  candidate_uuid: Uuid;
  symbol: string;
  instrument_type: InstrumentType;
  side: Side;
  candidate_kind: string;
  proposed_price: DecimalString | null;
  proposed_qty: DecimalString | null;
  confidence: number | null;
  rationale: string | null;
  currency: string | null;
  warnings: string[];
}

export interface PreopenReconciliationSummary {
  order_id: string;
  symbol: string;
  market: string;
  side: "buy" | "sell";
  classification: string;
  nxt_classification: string | null;
  nxt_actionable: boolean | null;
  gap_pct: DecimalString | null;
  summary: string | null;
  reasons: string[];
  warnings: string[];
}

export interface PreopenLinkedSession {
  session_uuid: Uuid;
  status: string;
  created_at: IsoDateTime;
}

export type PreopenNewsReadinessStatus = "ready" | "stale" | "unavailable";

export interface PreopenNewsSourceCoverage {
  feed_source: string;
  expected_count: number;
  stored_total: number;
  recent_24h: number;
  recent_6h: number;
  latest_published_at: IsoDateTime | null;
  latest_scraped_at: IsoDateTime | null;
  published_at_count: number;
  status: string;
  warnings: string[];
}

export interface PreopenNewsReadinessSummary {
  status: PreopenNewsReadinessStatus;
  is_ready: boolean;
  is_stale: boolean;
  latest_run_uuid: string | null;
  latest_status: string | null;
  latest_finished_at: IsoDateTime | null;
  latest_article_published_at: IsoDateTime | null;
  source_counts: Record<string, number>;
  source_coverage: PreopenNewsSourceCoverage[];
  warnings: string[];
  max_age_minutes: number;
}

export interface PreopenNewsArticlePreview {
  id: number;
  title: string;
  url: string;
  source: string | null;
  feed_source: string | null;
  published_at: IsoDateTime | null;
  summary: string | null;
}

export interface PreopenBriefingRelevance {
  score: number;
  reason: string;
  section_id: string | null;
  matched_terms: string[];
}

export interface PreopenMarketNewsItem {
  id: number;
  title: string;
  url: string;
  source: string | null;
  feed_source: string | null;
  published_at: IsoDateTime | null;
  summary: string | null;
  briefing_relevance: PreopenBriefingRelevance | null;
  crypto_relevance: Record<string, unknown> | null;
}

export interface PreopenMarketNewsSection {
  section_id: string;
  title: string;
  items: PreopenMarketNewsItem[];
}

export interface PreopenMarketNewsBriefing {
  briefing_filter: true;
  summary: Record<string, unknown>;
  sections: PreopenMarketNewsSection[];
  excluded_count: number;
  top_excluded: PreopenMarketNewsItem[];
}

export type PreopenArtifactStatus = "unavailable" | "draft" | "ready" | "degraded";
export type PreopenArtifactReadinessStatus =
  | "ready"
  | "stale"
  | "unavailable"
  | "partial";
export type PreopenDecisionSessionCtaState =
  | "unavailable"
  | "create_available"
  | "linked_session_exists";

export interface PreopenArtifactReadinessItem {
  key: string;
  status: PreopenArtifactReadinessStatus;
  is_ready: boolean;
  warnings: string[];
  details: Record<string, unknown>;
}

export interface PreopenArtifactSection {
  section_id: string;
  title: string;
  item_count: number;
  status: PreopenArtifactStatus;
  summary: string | null;
  items: Record<string, unknown>[];
}

export interface PreopenDecisionSessionCta {
  state: PreopenDecisionSessionCtaState;
  label: string;
  run_uuid: Uuid | null;
  linked_session_uuid: Uuid | null;
  disabled_reason: string | null;
  requires_confirmation: boolean;
}

export type PreopenPaperApprovalBridgeStatus =
  | "available"
  | "warning"
  | "blocked"
  | "unavailable";
export type PreopenPaperApprovalCandidateStatus =
  | "available"
  | "warning"
  | "unavailable";

export interface PreopenPaperApprovalCandidate {
  candidate_uuid: Uuid;
  symbol: string;
  status: PreopenPaperApprovalCandidateStatus;
  reason: string | null;
  warnings: string[];
  signal_symbol: string | null;
  signal_venue: string | null;
  execution_symbol: string | null;
  execution_venue: string | null;
  execution_asset_class: string | null;
  workflow_stage: string | null;
  purpose: string | null;
  preview_payload: CryptoPaperPreviewPayload | Record<string, unknown> | null;
  approval_copy: string[];
}

export interface PreopenPaperApprovalBridge {
  status: PreopenPaperApprovalBridgeStatus;
  generated_at: IsoDateTime | null;
  source: "deterministic_v1";
  preview_only: true;
  advisory_only: true;
  execution_allowed: false;
  market_scope: "kr" | "us" | "crypto" | null;
  stage: "preopen" | null;
  eligible_count: number;
  candidate_count: number;
  candidates: PreopenPaperApprovalCandidate[];
  blocking_reasons: string[];
  warnings: string[];
  unsupported_reasons: string[];
}

// ROB-100 contract types mirrored on the client.
export type ExecutionAccountMode =
  | "kis_live"
  | "kis_mock"
  | "alpaca_paper"
  | "db_simulated";
export type ExecutionSource =
  | "preopen"
  | "watch"
  | "manual"
  | "websocket"
  | "reconciler";
export type OrderLifecycleState =
  | "planned"
  | "previewed"
  | "submitted"
  | "accepted"
  | "pending"
  | "fill"
  | "reconciled"
  | "stale"
  | "failed"
  | "anomaly";

export interface ExecutionGuard {
  execution_allowed: boolean;
  approval_required: boolean;
  blocking_reasons: string[];
  warnings: string[];
}

export interface ExecutionReadiness {
  contract_version: "v1";
  account_mode: ExecutionAccountMode;
  execution_source: ExecutionSource;
  is_ready: boolean;
  guard: ExecutionGuard;
  checked_at: IsoDateTime | null;
  notes: string[];
}

export interface OrderPreviewLine {
  contract_version: "v1";
  symbol: string;
  market: string;
  side: "buy" | "sell";
  account_mode: ExecutionAccountMode;
  execution_source: ExecutionSource;
  lifecycle_state: OrderLifecycleState;
  quantity: DecimalString | null;
  limit_price: DecimalString | null;
  notional: DecimalString | null;
  currency: string | null;
  guard: ExecutionGuard;
  rationale: string[];
  correlation_id: string | null;
}

export interface OrderBasketPreview {
  contract_version: "v1";
  account_mode: ExecutionAccountMode;
  execution_source: ExecutionSource;
  readiness: ExecutionReadiness;
  lines: OrderPreviewLine[];
  basket_warnings: string[];
}

// ROB-101 execution review types.
export type ExecutionReviewStageId =
  | "data_news"
  | "candidate_review"
  | "cash_holdings_quotes"
  | "basket_preview"
  | "approval_required"
  | "post_order_reconcile";

export type ExecutionReviewStageStatus =
  | "ready"
  | "degraded"
  | "unavailable"
  | "skipped"
  | "pending";

export interface ExecutionReviewStage {
  stage_id: ExecutionReviewStageId;
  label: string;
  status: ExecutionReviewStageStatus;
  summary: string;
  warnings: string[];
  details: Record<string, unknown>;
}

export interface ExecutionReviewSummary {
  contract_version: "v1";
  advisory_only: true;
  execution_allowed: false;
  readiness: ExecutionReadiness;
  stages: ExecutionReviewStage[];
  basket_preview: OrderBasketPreview | null;
  blocking_reasons: string[];
  warnings: string[];
  notes: string[];
}

export type PreopenQaCheckStatus = "pass" | "warn" | "fail" | "unknown" | "skipped";
export type PreopenQaCheckSeverity = "info" | "low" | "medium" | "high";
export type PreopenQaGrade =
  | "excellent"
  | "good"
  | "watch"
  | "poor"
  | "unavailable";
export type PreopenQaConfidence = "high" | "medium" | "low" | "unavailable";
export type PreopenQaEvaluatorStatus =
  | "ready"
  | "needs_review"
  | "unavailable"
  | "skipped";

export interface PreopenQaCheck {
  id: string;
  label: string;
  status: PreopenQaCheckStatus;
  severity: PreopenQaCheckSeverity;
  summary: string;
  details: Record<string, unknown> | null;
}

export interface PreopenQaScore {
  score: number | null;
  grade: PreopenQaGrade;
  confidence: PreopenQaConfidence;
  reason: string | null;
}

export interface PreopenQaEvaluatorSummary {
  status: PreopenQaEvaluatorStatus;
  generated_at: IsoDateTime | null;
  source: "deterministic_v1";
  overall: PreopenQaScore;
  checks: PreopenQaCheck[];
  blocking_reasons: string[];
  warnings: string[];
  coverage: Record<string, unknown>;
}

export interface PreopenBriefingArtifact {
  artifact_type: "preopen_briefing";
  artifact_version: "v1";
  status: PreopenArtifactStatus;
  run_uuid: Uuid | null;
  market_scope: "kr" | "us" | "crypto" | null;
  stage: "preopen" | null;
  generated_at: IsoDateTime | null;
  source_run_status: string | null;
  readiness: PreopenArtifactReadinessItem[];
  market_summary: string | null;
  news_summary: string | null;
  sections: PreopenArtifactSection[];
  risk_notes: string[];
  cta: PreopenDecisionSessionCta;
  qa: Record<string, unknown>;
}

export interface PreopenLatestResponse {
  has_run: boolean;
  advisory_used: boolean;
  advisory_skipped_reason: string | null;
  run_uuid: Uuid | null;
  market_scope: "kr" | "us" | "crypto" | null;
  stage: "preopen" | null;
  status: string | null;
  strategy_name: string | null;
  source_profile: string | null;
  generated_at: IsoDateTime | null;
  created_at: IsoDateTime | null;
  notes: string | null;
  market_brief: Record<string, unknown> | null;
  source_freshness: Record<string, unknown> | null;
  source_warnings: string[];
  advisory_links: Record<string, unknown>[];
  candidate_count: number;
  reconciliation_count: number;
  candidates: PreopenCandidateSummary[];
  reconciliations: PreopenReconciliationSummary[];
  linked_sessions: PreopenLinkedSession[];
  news: PreopenNewsReadinessSummary | null;
  news_preview: PreopenNewsArticlePreview[];
  market_news_briefing: PreopenMarketNewsBriefing | null;
  briefing_artifact: PreopenBriefingArtifact | null;
  qa_evaluator: PreopenQaEvaluatorSummary | null;
  paper_approval_bridge: PreopenPaperApprovalBridge | null;
  execution_review: ExecutionReviewSummary | null;
}

export interface CreateFromResearchRunRequest {
  selector: { run_uuid: Uuid };
  include_tradingagents: false;
  notes: string;
}

export interface CreateFromResearchRunResponse {
  session_uuid: Uuid;
  session_url: string;
  status: string;
  advisory_skipped_reason: string | null;
  warnings: string[];
}

// Strategy events (ROB-41 backend, ROB-42 UI)
export type StrategyEventSource =
  | "user"
  | "hermes"
  | "tradingagents"
  | "news"
  | "market_data"
  | "scheduler";

export type StrategyEventType =
  | "operator_market_event"
  | "earnings_event"
  | "macro_event"
  | "sector_rotation"
  | "technical_break"
  | "risk_veto"
  | "cash_budget_change"
  | "position_change";

export interface StrategyEventDetail {
  id: number;
  event_uuid: Uuid;
  session_uuid: Uuid | null;
  source: StrategyEventSource;
  event_type: StrategyEventType;
  source_text: string;
  normalized_summary: string | null;
  affected_markets: string[];
  affected_sectors: string[];
  affected_themes: string[];
  affected_symbols: string[];
  severity: number;
  confidence: number;
  created_by_user_id: number | null;
  metadata: Record<string, unknown> | null;
  created_at: IsoDateTime;
}

export interface StrategyEventListResponse {
  events: StrategyEventDetail[];
  total: number;
  limit: number;
  offset: number;
}

export interface StrategyEventCreateRequest {
  source: "user";
  event_type: StrategyEventType;
  source_text: string;
  normalized_summary?: string;
  session_uuid?: Uuid;
  affected_markets?: string[];
  affected_sectors?: string[];
  affected_themes?: string[];
  affected_symbols?: string[];
  severity?: number;
  confidence?: number;
  metadata?: Record<string, unknown>;
}
