import type {
  FillListResponse,
  FillMarket,
  FillFreshnessReport,
  FillSide,
} from "../types/fills";

const BASE = "/trading/api/invest/fills";

export async function fetchRecentFills(
  limit = 50,
  market?: FillMarket,
  side?: FillSide,
): Promise<FillListResponse> {
  const q = new URLSearchParams({ limit: String(limit) });
  if (market) q.set("market", market);
  if (side) q.set("side", side);
  const res = await fetch(`${BASE}/recent?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`fills/recent ${res.status}`);
  return res.json();
}

export async function fetchFillsBySymbol(
  symbol: string,
  days = 30,
): Promise<FillListResponse> {
  const q = new URLSearchParams({ days: String(days) });
  const res = await fetch(`${BASE}/by-symbol/${encodeURIComponent(symbol)}?${q}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`fills/by-symbol ${res.status}`);
  return res.json();
}

export async function fetchSellHistory(opts?: {
  days?: number;
  market?: FillMarket;
  limit?: number;
}): Promise<FillListResponse> {
  const q = new URLSearchParams({
    days: String(opts?.days ?? 30),
    limit: String(opts?.limit ?? 100),
  });
  if (opts?.market) q.set("market", opts.market);
  const res = await fetch(`${BASE}/sell-history?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`fills/sell-history ${res.status}`);
  return res.json();
}

export async function fetchFillsFreshness(): Promise<FillFreshnessReport> {
  const res = await fetch(`${BASE}/freshness`, { credentials: "include" });
  if (!res.ok) throw new Error(`fills/freshness ${res.status}`);
  return res.json();
}
