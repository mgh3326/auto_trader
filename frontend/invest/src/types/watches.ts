export type WatchMarket = "all" | "kr" | "us" | "crypto";
export type WatchRowMarket = "kr" | "us" | "crypto";
export type WatchStatus = "all" | "active" | "triggered" | "expired" | "canceled";
export type WatchAlertStatus = "active" | "triggered" | "expired" | "canceled";
export type WatchProximityBand = "hit" | "within_0_5_pct" | "within_1_pct" | "outside";
export type WatchDataState = "ok" | "degraded" | "unavailable";

export interface WatchEventSummary {
  event_uuid: string;
  outcome: string;
  current_value: string | null;
  created_at: string;
}

export interface WatchAlertRow {
  alert_uuid: string;
  source_report_uuid: string;
  market: WatchRowMarket;
  symbol: string;
  symbol_name: string | null;
  target_kind: string;
  metric: string;
  operator: "above" | "below" | "between";
  threshold: string;
  threshold_high: string | null;
  status: WatchAlertStatus;
  valid_until: string;
  intent: string;
  action_mode: string;
  rationale: string;
  trigger_checklist: any[];
  max_action: Record<string, any>;
  current_price: string | null;
  proximity_band: WatchProximityBand | null;
  last_event: WatchEventSummary | null;
  near_expiry: boolean;
}

export interface WatchesResponse {
  market: WatchMarket;
  status: WatchStatus;
  count: number;
  data_state: WatchDataState;
  as_of: string;
  items: WatchAlertRow[];
  warnings: string[];
  empty_reason: string | null;
}
