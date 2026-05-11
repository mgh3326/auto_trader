import type { MarketDashboardResponse } from "../types/marketDashboard";

export async function fetchMarketDashboard(signal?: AbortSignal): Promise<MarketDashboardResponse> {
  const res = await fetch("/invest/api/market", { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`/invest/api/market ${res.status}`);
  }
  return res.json();
}
