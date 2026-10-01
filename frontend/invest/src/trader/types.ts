export type TraderBroker = "kis" | "toss" | "upbit";
export type TraderMarket = "kr" | "us" | "crypto";
export type TraderDataState = "ok" | "degraded" | "unavailable";
export type TraderSide = "buy" | "sell" | "unknown";

export interface TraderOpenOrderRow {
  broker: TraderBroker;
  market: TraderMarket;
  symbol: string;
  symbol_name: string | null;
  side: TraderSide;
  order_type: string | null;
  time_in_force: string | null;
  price: string | null;
  quantity: string | null;
  remaining_qty: string | null;
  filled_qty: string | null;
  status: string;
  raw_status: string | null;
  ordered_at: string | null;
  order_no: string;
  exchange: string | null;
  currency: string | null;
}

export interface TraderOpenOrderSource {
  broker: TraderBroker;
  market: TraderMarket;
  status: TraderDataState;
  count: number;
  message: string | null;
  fetched_at: string | null;
  last_ok_at: string | null;
}

export interface TraderOpenOrdersResponse {
  as_of: string;
  data_state: TraderDataState;
  count: number;
  items: TraderOpenOrderRow[];
  sources: TraderOpenOrderSource[];
  cache: { ttl_seconds: number; cached_at: string | null; hit: boolean };
  warnings: string[];
  notes: string[];
  empty_reason: string | null;
}

export interface TraderFillRow {
  id: number | null;
  broker: string;
  account_mode: string;
  venue: string;
  instrument_type: string;
  symbol: string;
  raw_symbol: string;
  side: string;
  broker_order_id: string;
  fill_seq: number;
  filled_qty: string;
  filled_price: string;
  filled_notional: string;
  fee_amount: string | null;
  fee_currency: string | null;
  filled_at: string;
  currency: string;
  correlation_id: string | null;
  source: string;
  source_run_id: string | null;
  created_at: string | null;
  updated_at: string | null;
  symbol_name: string | null;
  trade_day_kst: string;
}

export interface TraderFillsResponse {
  day_kst: string;
  window_start: string;
  window_end: string;
  count: number;
  items: TraderFillRow[];
  data_state: "fresh" | "stale" | "missing" | null;
  empty_reason: string | null;
  notes: string[];
}

export interface TraderWatchRow {
  alert_uuid: string;
  market: string;
  symbol: string;
  intent: string;
  metric: string;
  operator: string;
  threshold: string;
  threshold_high: string | null;
  valid_until: string;
  action_mode: string;
  status: string;
}

export interface TraderWatchesResponse {
  as_of: string;
  count: number;
  items: TraderWatchRow[];
}

// --- task 890 PR A: approval inbox (mirrors app/schemas/trader_approvals.py) ---

export interface TraderApprovalRung {
  rung_index: number;
  side: string;
  quantity: string;
  limit_price: string | null;
  notional: string | null;
  /** acked/resting = broker accepted, unverified = unknown, rejected = refused. */
  state: string;
  broker_order_id: string | null;
  filled_qty: string | null;
  void_reason: string | null;
  distance_pct: string | null;
}

export interface TraderApprovalItem {
  proposal_id: string;
  symbol: string;
  market: string;
  account_mode: string;
  broker_account_id: string | null;
  side: string;
  order_type: string;
  action: string;
  exit_intent: string | null;
  requires_two_step: boolean;
  card_kind: string | null;
  lifecycle_state: string;
  rungs: TraderApprovalRung[];
  total_quantity: string | null;
  total_notional: string | null;
  distance_pct: string | null;
  distance_price_asof: string | null;
  tier: string | null;
  caveats: string[];
  valid_until: string | null;
  expires_in_seconds: number | null;
  approved_at: string | null;
  approved_by_channel: string | null;
  commit_lease_active: boolean;
  actionable: boolean;
  block_reason: string | null;
}

export interface TraderApprovalInboxResponse {
  as_of: string;
  actions_enabled: boolean;
  loss_cut_actions_enabled: boolean;
  count: number;
  items: TraderApprovalItem[];
}

export interface TraderApprovalDetailResponse {
  as_of: string;
  actions_enabled: boolean;
  loss_cut_actions_enabled: boolean;
  item: TraderApprovalItem;
}
