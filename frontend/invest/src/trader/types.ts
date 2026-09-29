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
