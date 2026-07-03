import type {
  NextActionsResponse,
  RetroMarket,
  RetrospectivesResponse,
} from "../types/retrospectives";

const BASE = "/trading/api/invest/retrospectives";

export interface RetrospectivesQuery {
  market?: RetroMarket;
  triggerType?: string;
  rootCauseClass?: string;
  symbol?: string;
  days?: number;
  limit?: number;
  offset?: number;
}

export async function fetchRetrospectives(
  q: RetrospectivesQuery,
): Promise<RetrospectivesResponse> {
  const params = new URLSearchParams({ market: q.market ?? "all" });
  if (q.triggerType) params.set("trigger_type", q.triggerType);
  if (q.rootCauseClass) params.set("root_cause_class", q.rootCauseClass);
  if (q.symbol) params.set("symbol", q.symbol);
  if (q.days != null) params.set("days", String(q.days));
  if (q.limit != null) params.set("limit", String(q.limit));
  if (q.offset != null) params.set("offset", String(q.offset));
  const res = await fetch(`${BASE}?${params}`, { credentials: "include" });
  if (!res.ok) throw new Error(`retrospectives ${res.status}`);
  return res.json();
}

export async function fetchOpenNextActions(
  market: RetroMarket = "all",
  symbol?: string,
  status?: string,
): Promise<NextActionsResponse> {
  const params = new URLSearchParams({ market });
  if (symbol) params.set("symbol", symbol);
  if (status) params.set("status", status);
  const res = await fetch(`${BASE}/next-actions?${params}`, { credentials: "include" });
  if (!res.ok) throw new Error(`retrospectives next-actions ${res.status}`);
  return res.json();
}
