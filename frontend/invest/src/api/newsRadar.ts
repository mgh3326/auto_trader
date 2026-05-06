// frontend/invest/src/api/newsRadar.ts
import type { NewsRadarMarket, NewsRadarResponse } from "../types/newsRadar";

export interface FetchNewsRadarParams {
  market?: NewsRadarMarket;
  hours?: number;
  limit?: number;
  includeExcluded?: boolean;
}

export async function fetchNewsRadar(
  params: FetchNewsRadarParams = {},
  signal?: AbortSignal,
): Promise<NewsRadarResponse> {
  const qs = new URLSearchParams({
    market: params.market ?? "all",
    hours: String(params.hours ?? 24),
    include_excluded: String(params.includeExcluded ?? true),
    limit: String(params.limit ?? 20),
  });
  const res = await fetch(`/trading/api/news-radar?${qs.toString()}`, {
    credentials: "include",
    signal,
  });
  if (!res.ok) {
    throw new Error(`/trading/api/news-radar ${res.status}`);
  }
  return (await res.json()) as NewsRadarResponse;
}
