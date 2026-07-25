// Read-only forecast calibration surface (ROB-663).
// Mirrors app/schemas/invest_forecasts.py field-for-field (snake_case preserved).

export type ForecastGroupBy = "created_by" | "session_label" | "model_label" | "day";

export interface CalibrationGroupRow {
  group: string;
  sample_size: number;
  hits: number;
  misses: number;
  hit_rate: number | null;
  avg_brier_score: number | null;
  avg_probability: number | null;
  calibration_gap: number | null;
}

export interface CalibrationResponse {
  group_by: string;
  created_by: string | null;
  symbol: string | null;
  instrument_type: string | null;
  days: number | null;
  count: number;
  groups: CalibrationGroupRow[];
  as_of: string;
}

export interface ForecastRow {
  id: number;
  forecast_id: string;
  correlation_id: string | null;
  created_by: string | null;
  session_label: string | null;
  model_label: string | null;
  symbol: string;
  instrument_type: string | null;
  forecast_target: Record<string, unknown> | null;
  horizon: string | null;
  probability: number | null;
  probability_range_low: number | null;
  probability_range_high: number | null;
  resolution_source: string | null;
  review_date: string | null;
  status: string | null;
  outcome: boolean | null;
  observed_value: number | null;
  resolved_at: string | null;
  brier_score: number | null;
  created_at: string | null;
}

export interface ForecastListResponse {
  kind: "open" | "closed";
  symbol: string | null;
  created_by: string | null;
  instrument_type: string | null;
  count: number;
  items: ForecastRow[];
  as_of: string;
}
