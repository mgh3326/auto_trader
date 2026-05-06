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

export type CommitteeTraderAction =
  | "BUY"
  | "HOLD"
  | "TRIM"
  | "SELL"
  | "AVOID"
  | "WATCH"
  | "REBALANCE";

export interface CommitteeDebateClaim {
  text: string;
  weight: "low" | "medium" | "high";
  source: "technical" | "news" | "portfolio" | "fundamentals" | "sentiment";
}

export interface CommitteeResearchDebate {
  bull_case: CommitteeDebateClaim[];
  bear_case: CommitteeDebateClaim[];
  summary: string | null;
}

export interface CommitteeTraderDraft {
  symbol: string;
  action: CommitteeTraderAction;
  price_plan: string | null;
  size_plan: string | null;
  rationale: string | null;
  confidence: "low" | "medium" | "high";
  invalidation_condition: string | null;
  next_step_recommendation: string | null;
  is_live_order: false;
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
  research_debate: CommitteeResearchDebate | null;
  trader_draft: CommitteeTraderDraft[] | null;
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

// News Radar (ROB-109)
export type NewsRadarMarket = "all" | "kr" | "us" | "crypto";
export type NewsRadarSeverity = "high" | "medium" | "low";
export type NewsRadarReadinessStatus = "ready" | "stale" | "unavailable";
export type NewsRadarRiskCategory =
  | "geopolitical_oil"
  | "macro_policy"
  | "crypto_security"
  | "earnings_bigtech"
  | "korea_market";

export interface NewsRadarReadiness {
  status: NewsRadarReadinessStatus;
  latest_scraped_at: IsoDateTime | null;
  latest_published_at: IsoDateTime | null;
  recent_6h_count: number;
  recent_24h_count: number;
  source_count: number;
  stale: boolean;
  max_age_minutes: number;
  warnings: string[];
}

export interface NewsRadarSummary {
  high_risk_count: number;
  total_count: number;
  included_in_briefing_count: number;
  excluded_but_collected_count: number;
}

export interface NewsRadarSourceCoverage {
  feed_source: string;
  recent_6h: number;
  recent_24h: number;
  latest_published_at: IsoDateTime | null;
  latest_scraped_at: IsoDateTime | null;
  status: string;
}

export interface NewsRadarItem {
  id: string;
  title: string;
  source: string | null;
  feed_source: string | null;
  url: string;
  published_at: IsoDateTime | null;
  market: string;
  risk_category: NewsRadarRiskCategory | null;
  severity: NewsRadarSeverity;
  themes: string[];
  symbols: string[];
  included_in_briefing: boolean;
  briefing_reason: string | null;
  briefing_score: number;
  snippet: string | null;
  matched_terms: string[];
}

export interface NewsRadarSection {
  section_id: NewsRadarRiskCategory;
  title: string;
  severity: NewsRadarSeverity;
  items: NewsRadarItem[];
}

export interface NewsRadarFilters {
  market: NewsRadarMarket;
  hours: number;
  q: string;
  riskCategory: NewsRadarRiskCategory | "";
  includeExcluded: boolean;
  limit: number;
}

export interface NewsRadarResponse {
  market: NewsRadarMarket;
  as_of: IsoDateTime;
  readiness: NewsRadarReadiness;
  summary: NewsRadarSummary;
  sections: NewsRadarSection[];
  items: NewsRadarItem[];
  excluded_items: NewsRadarItem[];
  source_coverage: NewsRadarSourceCoverage[];
}

// --- ROB-113 Research Pipeline Phase 3 types ---

export type ResearchSessionStatus =
  | "open"
  | "running"
  | "finalized"
  | "failed"
  | "cancelled";

export type StageType = "market" | "news" | "fundamentals" | "social";

export type StageVerdict = "bull" | "bear" | "neutral" | "unavailable";

export type SummaryDecision = "buy" | "hold" | "sell";

export type LinkDirection = "support" | "contradict" | "context";

export type ResearchInstrumentType = "equity_kr" | "equity_us" | "crypto";

export interface ResearchSessionListItem {
  id: number;
  stock_info_id: number;
  status: ResearchSessionStatus | string;
  created_at: IsoDateTime;
  decision: SummaryDecision | null;
  confidence: number | null;
}

export interface ResearchSessionHeader {
  id: number;
  stock_info_id: number;
  research_run_id: number | null;
  status: ResearchSessionStatus | string;
  started_at: IsoDateTime | null;
  finalized_at: IsoDateTime | null;
  created_at: IsoDateTime;
  updated_at: IsoDateTime | null;
  symbol: string | null;
  instrument_type: string | null;
}

export interface MarketSignals {
  last_close?: number;
  change_pct?: number;
  rsi_14?: number;
  atr_14?: number;
  volume_ratio_20d?: number;
  trend?: "uptrend" | "downtrend" | "flat" | "unknown";
  price_change_pct_1d?: number;
  price_change_pct_5d?: number;
  price_change_pct_20d?: number;
  macd_signal?: string;
  bollinger_position?: string;
  supports?: number[];
  resistances?: number[];
  trend_short?: string;
  trend_mid?: string;
  trend_long?: string;
  [key: string]: unknown;
}

export interface NewsSignals {
  headline_count?: number;
  sentiment_score?: number;
  top_themes?: string[];
  urgent_flags?: string[];
  articles?: Array<{
    title?: string;
    url?: string;
    source?: string;
    published_at?: IsoDateTime;
    sentiment?: "positive" | "negative" | "neutral";
  }>;
  [key: string]: unknown;
}

export interface FundamentalsSignals {
  per?: number | null;
  pbr?: number | null;
  peg?: number | null;
  ev_ebitda?: number | null;
  market_cap?: number | null;
  sector?: string | null;
  peer_count?: number;
  relative_per_vs_peers?: number | null;
  disclosures?: Array<{ title: string; url?: string; reported_at?: IsoDateTime }>;
  analyst_consensus?: string | null;
  insider_flow?: string | null;
  [key: string]: unknown;
}

export interface SocialSignals {
  available: boolean;
  reason: string;
  phase: string;
  [key: string]: unknown;
}

export interface SourceFreshness {
  newest_age_minutes: number;
  oldest_age_minutes: number;
  missing_sources: string[];
  stale_flags: string[];
  source_count: number;
}

export interface StageAnalysis {
  id: number;
  stage_type: StageType;
  verdict: StageVerdict;
  confidence: number;
  signals: MarketSignals | NewsSignals | FundamentalsSignals | SocialSignals;
  raw_payload: Record<string, unknown> | null;
  source_freshness: SourceFreshness | null;
  executed_at: IsoDateTime;
  snapshot_at: IsoDateTime | null;
}

export interface BullBearArgument {
  text: string;
  cited_stage_ids: number[];
  direction: LinkDirection;
  weight: number;
}

export interface PriceAnalysis {
  appropriate_buy_min?: number | null;
  appropriate_buy_max?: number | null;
  appropriate_sell_min?: number | null;
  appropriate_sell_max?: number | null;
  buy_hope_min?: number | null;
  buy_hope_max?: number | null;
  sell_target_min?: number | null;
  sell_target_max?: number | null;
}

export interface SummaryStageLink {
  stage_analysis_id: number;
  stage_type: StageType;
  direction: LinkDirection;
  weight: number;
  rationale: string | null;
}

export interface ResearchSummary {
  id: number;
  session_id: number;
  decision: SummaryDecision;
  confidence: number;
  bull_arguments: BullBearArgument[];
  bear_arguments: BullBearArgument[];
  price_analysis: PriceAnalysis | null;
  reasons: string[] | null;
  detailed_text: string | null;
  warnings: string[] | null;
  executed_at: IsoDateTime;
  summary_stage_links: SummaryStageLink[];
}

export interface ResearchSessionFullResponse {
  session: ResearchSessionHeader;
  stages: StageAnalysis[];
  summary: ResearchSummary | null;
}

export interface ResearchSessionCreateRequest {
  symbol: string;
  name?: string | null;
  instrument_type: ResearchInstrumentType;
  research_run_id?: number | null;
  triggered_by?: "user" | "scheduler";
}

export interface ResearchSessionCreateResponse {
  session_id: number;
  status: ResearchSessionStatus;
  started_at: IsoDateTime;
}

export interface SymbolTimelineEntry {
  session_id: number;
  status: ResearchSessionStatus | string;
  started_at: IsoDateTime | null;
  finalized_at: IsoDateTime | null;
  decision: SummaryDecision | null;
  confidence: number | null;
  stage_verdicts: Partial<Record<StageType, StageVerdict>>;
}

export interface SymbolTimelineResponse {
  symbol: string;
  days: number;
  entries: SymbolTimelineEntry[];
}

// === ROB-116 portfolio actions ===

export type CandidateAction = "sell" | "trim" | "hold" | "add" | "watch";
export type MarketVerdict = "bull" | "bear" | "neutral" | "unavailable";
export type JournalStatus = "present" | "missing" | "stale";
export type Market = "KR" | "US" | "CRYPTO";

export interface PortfolioActionCandidate {
  symbol: string;
  name: string | null;
  market: Market;
  instrument_type: string | null;
  position_weight_pct: number | null;
  profit_rate: number | null;
  quantity: number | null;
  sellable_quantity: number | null;
  staked_quantity: number | null;
  latest_research_session_id: number | null;
  summary_decision: SummaryDecision | null;
  summary_confidence: number | null;
  market_verdict: MarketVerdict | null;
  nearest_support_pct: number | null;
  nearest_resistance_pct: number | null;
  journal_status: JournalStatus;
  candidate_action: CandidateAction;
  suggested_trim_pct: number | null;
  reason_codes: string[];
  missing_context_codes: string[];
}

export interface PortfolioActionsResponse {
  generated_at: string;
  total: number;
  candidates: PortfolioActionCandidate[];
  warnings: string[];
}

// === ROB-117 candidate discovery ===

export type CandidateMarket =
  | "kr" | "kospi" | "kosdaq" | "konex" | "all" | "us" | "crypto";
export type CandidateStrategy = "oversold" | "momentum" | "high_volume";
export type CandidateSortBy =
  | "volume" | "trade_amount" | "market_cap" | "change_rate" | "dividend_yield" | "rsi";
export type ResearchStatus = "new" | "watch" | "exclude";

export interface CandidateScreenRequest {
  market: CandidateMarket;
  asset_type?: "stock" | "etf" | "etn" | null;
  strategy?: CandidateStrategy | null;
  sort_by?: CandidateSortBy | null;
  sort_order?: "asc" | "desc";
  min_market_cap?: number | null;
  max_per?: number | null;
  max_pbr?: number | null;
  min_dividend_yield?: number | null;
  max_rsi?: number | null;
  adv_krw_min?: number | null;
  market_cap_min_krw?: number | null;
  market_cap_max_krw?: number | null;
  exclude_sectors?: string[] | null;
  instrument_types?: string[] | null;
  krw_only?: boolean;
  exclude_warnings?: boolean;
  limit?: number;
}

export interface ScreenedCandidate {
  symbol: string;
  name: string | null;
  market: string | null;
  instrument_type: string | null;
  price: number | null;
  change_rate: number | null;
  volume: number | null;
  trade_amount_24h: number | null;
  volume_ratio: number | null;
  rsi: number | null;
  market_cap: number | null;
  per: number | null;
  pbr: number | null;
  sector: string | null;
  is_held: boolean;
  held_quantity: number | null;
  latest_research_session_id: number | null;
  research_status: ResearchStatus | null;
  data_warnings: string[];
}

export interface CandidateScreenResponse {
  generated_at: string;
  market: string;
  strategy: string | null;
  sort_by: string | null;
  total: number;
  candidates: ScreenedCandidate[];
  warnings: string[];
  rsi_enrichment_attempted: number;
  rsi_enrichment_succeeded: number;
}


// ROB-120 — Trade journal DTOs
export type JournalStatusValue =
  | "draft"
  | "active"
  | "closed"
  | "stopped"
  | "expired";
export type WritableJournalStatus = "draft" | "active";
export type JournalCoverageStatus = "present" | "missing" | "stale";

export interface JournalCoverageRow {
  symbol: string;
  name: string | null;
  market: Market;
  instrument_type: string | null;
  quantity: number | null;
  position_weight_pct: number | null;
  journal_status: JournalCoverageStatus;
  journal_id: number | null;
  thesis: string | null;
  target_price: number | null;
  stop_loss: number | null;
  min_hold_days: number | null;
  hold_until: string | null;
  latest_research_session_id: number | null;
  latest_research_summary_id: number | null;
  latest_summary_decision: SummaryDecision | null;
  thesis_conflict_with_summary: boolean;
}

export interface JournalCoverageResponse {
  generated_at: string;
  total: number;
  rows: JournalCoverageRow[];
  warnings: string[];
}

export interface JournalReadResponse {
  id: number;
  symbol: string;
  instrument_type: string;
  side: "buy" | "sell";
  thesis: string;
  strategy: string | null;
  target_price: number | null;
  stop_loss: number | null;
  min_hold_days: number | null;
  hold_until: string | null;
  status: JournalStatusValue;
  account: string | null;
  account_type: "live" | "paper";
  notes: string | null;
  research_session_id: number | null;
  research_summary_id: number | null;
  pnl_pct?: number | null;
  created_at: string;
  updated_at: string;
}

export interface JournalCreateRequest {
  symbol: string;
  instrument_type: string;
  side?: "buy" | "sell";
  thesis: string;
  strategy?: string | null;
  target_price?: number | null;
  stop_loss?: number | null;
  min_hold_days?: number | null;
  status?: WritableJournalStatus;
  account?: string | null;
  notes?: string | null;
  research_session_id?: number | null;
  research_summary_id?: number | null;
}

export interface JournalUpdateRequest {
  thesis?: string;
  strategy?: string | null;
  target_price?: number | null;
  stop_loss?: number | null;
  min_hold_days?: number | null;
  status?: WritableJournalStatus;
  notes?: string | null;
  research_session_id?: number | null;
  research_summary_id?: number | null;
}

// ROB-121 — Research retrospective DTOs
export type DecisionVerdict = "buy" | "hold" | "sell";

export interface RetrospectiveStageCoverageStat {
  stage_type: StageType;
  coverage_pct: number;
  stale_pct: number;
  unavailable_pct: number;
}

export interface RetrospectiveDecisionDistribution {
  ai_buy: number;
  ai_hold: number;
  ai_sell: number;
  user_accept: number;
  user_reject: number;
  user_modify: number;
  user_defer: number;
  user_pending: number;
}

export interface RetrospectivePnlSummary {
  realized_pnl_pct_avg: number | null;
  unrealized_pnl_pct_avg: number | null;
  sample_size: number;
}

export interface RetrospectiveOverview {
  period_start: string;
  period_end: string;
  market: Market | null;
  strategy: string | null;
  sessions_total: number;
  summaries_total: number;
  decision_distribution: RetrospectiveDecisionDistribution;
  stage_coverage: RetrospectiveStageCoverageStat[];
  pnl: RetrospectivePnlSummary;
  warnings: string[];
}

export interface RetrospectiveStagePerformanceRow {
  stage_combo: string;
  sample_size: number;
  win_rate_pct: number | null;
  avg_realized_pnl_pct: number | null;
}

export interface RetrospectiveDecisionRow {
  research_session_id: number;
  symbol: string;
  market: Market;
  decided_at: string;
  ai_decision: DecisionVerdict | null;
  user_response: string | null;
  realized_pnl_pct: number | null;
  proposal_id: number | null;
}

export interface RetrospectiveDecisionsResponse {
  total: number;
  rows: RetrospectiveDecisionRow[];
}
// === ROB-118 order preview ===

export type OrderPreviewStatus =
  | "created"
  | "preview_passed"
  | "preview_failed"
  | "submitted"
  | "submit_failed"
  | "canceled";

export type OrderPreviewLeg = {
  leg_index: number;
  quantity: string;
  price: string | null;
  order_type: "limit" | "market";
  estimated_value: string | null;
  estimated_fee: string | null;
  expected_pnl: string | null;
  dry_run_status: "passed" | "failed" | "skipped" | null;
  dry_run_error: Record<string, unknown> | null;
};

export type OrderExecutionRequest = {
  leg_index: number;
  broker_order_id: string | null;
  status: "submitted" | "rejected" | "failed";
  error_payload: Record<string, unknown> | null;
  submitted_at: string;
};

export type OrderPreviewSession = {
  preview_uuid: string;
  source_kind: "portfolio_action" | "candidate" | "research_run";
  source_ref: string | null;
  research_session_id: string | null;
  symbol: string;
  market: "equity_kr" | "equity_us" | "crypto";
  venue: string;
  side: "buy" | "sell";
  status: OrderPreviewStatus;
  approval_token: string | null;
  legs: OrderPreviewLeg[];
  executions: OrderExecutionRequest[];
  dry_run_error: Record<string, unknown> | null;
  approved_at: string | null;
  submitted_at: string | null;
  created_at: string;
  updated_at: string;
};

export type CreateOrderPreviewRequest = {
  source_kind: OrderPreviewSession["source_kind"];
  source_ref?: string | null;
  research_session_id?: string | null;
  symbol: string;
  market: OrderPreviewSession["market"];
  venue: string;
  side: "buy" | "sell";
  legs: Array<{
    leg_index: number;
    quantity: string;
    price?: string | null;
    order_type?: "limit" | "market";
  }>;
};

