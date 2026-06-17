import type { WatchesResponse, WatchMarket, WatchStatus } from "../types/watches";

const BASE = "/trading/api/invest/watches";

export async function fetchWatches(
  market: WatchMarket = "all",
  status: WatchStatus = "all",
  symbol?: string,
): Promise<WatchesResponse> {
  const q = new URLSearchParams({ market, status });
  // ROB-592: per-symbol scope for the stock detail watch card.
  if (symbol) q.set("symbol", symbol);
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`watches ${res.status}`);
  return res.json();
}
