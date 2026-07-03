export type RetroMarket = "all" | "kr" | "us" | "crypto";

export interface RetrospectiveRow {
  id: number;
  correlation_id: string | null;
  symbol: string;
  market: string | null;
  instrument_type: string | null;
  side: string | null;
  trigger_type: string | null;
  root_cause_class: string | null;
  outcome: string | null;
  realized_pnl: number | null;
  realized_pnl_currency: string | null;
  pnl_pct: number | null;
  result_summary: string | null;
  lesson: string | null;
  next_strategy: string | null;
  intended_vs_happened: Record<string, unknown> | null;
  next_actions: Array<Record<string, unknown>> | null;
  guardrail_fired: boolean | null;
  policy_version: string | null;
  created_at: string | null;
}

export interface RetrospectivesResponse {
  market: RetroMarket;
  trigger_type: string | null;
  root_cause_class: string | null;
  symbol: string | null;
  count: number;
  total: number;
  items: RetrospectiveRow[];
  as_of: string;
}

export interface NextActionRow {
  action: string;
  owner: string | null;
  issue_id: string | null;
  status: string | null;
  due_kst_date: string | null;
  symbol: string;
  market: string | null;
  retro_id: number;
  correlation_id: string | null;
  trigger_type: string | null;
  realized_pnl: number | null;
  created_at: string | null;
}

export interface NextActionsResponse {
  market: RetroMarket;
  symbol: string | null;
  count: number;
  scan_limit: number;
  items: NextActionRow[];
}
