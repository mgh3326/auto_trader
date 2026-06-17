import type { WatchesResponse, WatchMarket, WatchStatus } from "../types/watches";

const BASE = "/trading/api/invest/watches";

export async function fetchWatches(
  market: WatchMarket = "all",
  status: WatchStatus = "all",
): Promise<WatchesResponse> {
  const q = new URLSearchParams({ market, status });
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`watches ${res.status}`);
  return res.json();
}
