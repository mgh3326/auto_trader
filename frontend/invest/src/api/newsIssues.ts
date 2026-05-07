// frontend/invest/src/api/newsIssues.ts
import type { MarketIssuesMarketFilter, MarketIssuesResponse } from "../types/newsIssues";

export interface FetchNewsIssuesParams {
  market?: MarketIssuesMarketFilter;
  windowHours?: number;
  limit?: number;
}

export async function fetchNewsIssues(
  params: FetchNewsIssuesParams = {},
  signal?: AbortSignal,
): Promise<MarketIssuesResponse> {
  const qs = new URLSearchParams({
    market: params.market ?? "all",
    window_hours: String(params.windowHours ?? 24),
    limit: String(params.limit ?? 20),
  });
  const res = await fetch(`/trading/api/news-issues?${qs.toString()}`, {
    credentials: "include",
    signal,
  });
  if (!res.ok) {
    throw new Error(`/trading/api/news-issues ${res.status}`);
  }
  return (await res.json()) as MarketIssuesResponse;
}
