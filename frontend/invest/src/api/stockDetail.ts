import type {
  StockDetailCandlesResponse,
  StockDetailMarket,
  StockDetailNewsResponse,
  StockDetailOrdersResponse,
  StockDetailRecommendationResponse,
  StockDetailResearchConsensusResponse,
  StockDetailResponse,
} from "../types/stockDetail";
import { normalizeLinkedOrder } from "./investmentReports";
import type { LinkedOrder } from "../types/investmentReports";

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return res.json();
}

function stockDetailPath(market: StockDetailMarket, symbol: string): string {
  return `/invest/api/stock-detail/${encodeURIComponent(market)}/${encodeURIComponent(symbol)}`;
}

export async function fetchStockDetail(params: {
  market: StockDetailMarket;
  symbol: string;
}): Promise<StockDetailResponse> {
  return getJson<StockDetailResponse>(stockDetailPath(params.market, params.symbol));
}

export async function fetchStockDetailResearchConsensus(params: {
  market: StockDetailMarket;
  symbol: string;
}): Promise<StockDetailResearchConsensusResponse> {
  return getJson<StockDetailResearchConsensusResponse>(`${stockDetailPath(params.market, params.symbol)}/research-consensus`);
}

// ROB-692 — on-demand deterministic recommendation (action/confidence/
// buy_zones/sell_targets/stop_loss/reasoning + optional R:R trade_setup).
// Crypto is unsupported at the router (400) — callers should gate on
// market !== "crypto" before calling, same as research-consensus.
export async function fetchStockDetailRecommendation(params: {
  market: StockDetailMarket;
  symbol: string;
}): Promise<StockDetailRecommendationResponse> {
  return getJson<StockDetailRecommendationResponse>(`${stockDetailPath(params.market, params.symbol)}/recommendation`);
}

export async function fetchStockDetailCandles(params: {
  market: StockDetailMarket;
  symbol: string;
  period?: string;
}): Promise<StockDetailCandlesResponse> {
  const q = new URLSearchParams();
  if (params.period) q.set("period", params.period);
  const qs = q.toString();
  const suffix = qs ? `?${qs}` : "";
  return getJson<StockDetailCandlesResponse>(`${stockDetailPath(params.market, params.symbol)}/candles${suffix}`);
}

export async function fetchStockDetailNews(params: {
  market: StockDetailMarket;
  symbol: string;
  limit?: number;
  cursor?: string;
}): Promise<StockDetailNewsResponse> {
  const q = new URLSearchParams();
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.cursor) q.set("cursor", params.cursor);
  const qs = q.toString();
  const suffix = qs ? `?${qs}` : "";
  return getJson<StockDetailNewsResponse>(`${stockDetailPath(params.market, params.symbol)}/news${suffix}`);
}

export async function fetchStockDetailOrders(params: {
  market: StockDetailMarket;
  symbol: string;
  cursor?: string;
}): Promise<StockDetailOrdersResponse> {
  const q = new URLSearchParams();
  if (params.cursor) q.set("cursor", params.cursor);
  const qs = q.toString();
  const suffix = qs ? `?${qs}` : "";
  return getJson<StockDetailOrdersResponse>(`${stockDetailPath(params.market, params.symbol)}/orders${suffix}`);
}

// ROB-559 — per-symbol live order history (status + rationale + fill rollup).
// Backend returns snake_case LinkedOrderView rows; normalize to camelCase
// LinkedOrder with the same mapper the report bundle uses. Crypto sends the raw
// Upbit pair (e.g. KRW-BTC) as-is — it matches LiveOrderLedger.symbol directly.
export async function fetchStockDetailOrderLedger(params: {
  market: StockDetailMarket;
  symbol: string;
  days?: number;
}): Promise<LinkedOrder[]> {
  const q = new URLSearchParams();
  if (params.days !== undefined) q.set("days", String(params.days));
  const qs = q.toString();
  const suffix = qs ? `?${qs}` : "";
  const raw = await getJson<{ count: number; items: Record<string, unknown>[] }>(
    `${stockDetailPath(params.market, params.symbol)}/order-ledger${suffix}`,
  );
  return (raw.items ?? []).map(normalizeLinkedOrder);
}
