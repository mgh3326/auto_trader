export type Uuid = string;
export type IsoDateTime = string;
export type DecimalString = string;

export type SessionStatus = "open" | "closed" | "archived";
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
  original_payload: Record<string, unknown>;
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
  proposals: ProposalDetail[];
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
