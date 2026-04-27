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
