// frontend/trading-decision/src/api/newsRadar.ts
import { apiFetch } from "./client";
import type { NewsRadarFilters, NewsRadarResponse } from "./types";

export function getNewsRadar(
  filters: NewsRadarFilters,
): Promise<NewsRadarResponse> {
  const params = new URLSearchParams();
  params.set("market", filters.market);
  params.set("hours", String(filters.hours));
  if (filters.q.trim()) params.set("q", filters.q.trim());
  if (filters.riskCategory) params.set("risk_category", filters.riskCategory);
  params.set("include_excluded", filters.includeExcluded ? "true" : "false");
  params.set("limit", String(filters.limit));
  return apiFetch<NewsRadarResponse>(`/news-radar?${params.toString()}`);
}
