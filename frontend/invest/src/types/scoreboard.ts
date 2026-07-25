// Read-only judgment scoreboard surface (ROB-691).
// Mirrors app/schemas/invest_retrospectives.py Scoreboard* models field-for-field
// (snake_case preserved).

export type ScoreboardGroupBy = "strategy" | "day" | "trigger_type" | "root_cause";

export interface ScoreboardGroupRow {
  group: string;
  sample_size: number;
  wins: number;
  misses: number;
  win_rate_pct: number | null;
  avg_pnl_pct: number | null;
  realized_pnl_sum: Record<string, number>;
  fx_pnl_krw_sum: number;
  total_pnl_krw_sum: number;
  by_outcome: Record<string, number>;
  by_trigger_type: Record<string, number>;
  by_root_cause_class: Record<string, number>;
}

export interface ScoreboardTotals {
  sample_size: number;
  wins: number;
  misses: number;
  decided: number;
  win_rate_pct: number | null;
  realized_pnl_sum: Record<string, number>;
  fx_pnl_krw_sum: number;
  total_pnl_krw_sum: number;
  excluded_no_fill_evidence: number;
}

export interface ScoreboardResponse {
  group_by: string;
  market: "all" | "kr" | "us" | "crypto";
  kst_date_from: string | null;
  kst_date_to: string | null;
  count: number;
  groups: ScoreboardGroupRow[];
  totals: ScoreboardTotals;
  as_of: string;
}
