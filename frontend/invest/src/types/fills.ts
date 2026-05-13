export type FillMarket = "kr" | "us" | "crypto";
export type FillSide = "buy" | "sell";
export type FillDataState = "fresh" | "stale" | "missing";

export interface FillRow {
  id: number | null;
  broker: string;
  account_mode: string;
  venue: string;
  instrument_type: string;
  symbol: string;
  /** Optional display name supplied by backend or future broker/Toss enrichment. */
  symbol_name?: string | null;
  /** Camel-case alias tolerated for client-side/backward-compatible enrichers. */
  symbolName?: string | null;
  raw_symbol: string;
  side: FillSide;
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
}

export interface FillSourceBreakdown {
  reconciler: number;
  websocket: number;
  manual_import: number;
}

export interface FillListResponse {
  count: number;
  items: FillRow[];
  data_state: FillDataState | null;
  source_breakdown: FillSourceBreakdown | null;
  empty_reason: string | null;
}

export interface FillFreshnessEntry {
  broker: string;
  last_run_at: string | null;
  lag_minutes: number | null;
  dataState: FillDataState;
  last_run_id: string | null;
  notes: string | null;
}

export interface FillFreshnessReport {
  items: FillFreshnessEntry[];
}
