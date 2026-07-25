import type { ScoreboardGroupBy, ScoreboardResponse } from "../types/scoreboard";

const BASE = "/trading/api/invest/retrospectives/scoreboard";

export interface ScoreboardQuery {
  groupBy?: ScoreboardGroupBy;
  market?: "all" | "kr" | "us" | "crypto";
  accountMode?: string;
  strategyKey?: string;
  dateFrom?: string;
  dateTo?: string;
}

export async function fetchScoreboard(
  q: ScoreboardQuery = {},
): Promise<ScoreboardResponse> {
  const params = new URLSearchParams();
  if (q.groupBy) params.set("group_by", q.groupBy);
  if (q.market) params.set("market", q.market);
  if (q.accountMode) params.set("account_mode", q.accountMode);
  if (q.strategyKey) params.set("strategy_key", q.strategyKey);
  if (q.dateFrom) params.set("kst_date_from", q.dateFrom);
  if (q.dateTo) params.set("kst_date_to", q.dateTo);
  const qs = params.toString();
  const res = await fetch(`${BASE}${qs ? `?${qs}` : ""}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`scoreboard ${res.status}`);
  return res.json();
}
