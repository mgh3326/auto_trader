import type {
  StockDetailCandlesResponse,
  StockDetailMarket,
  StockDetailNewsResponse,
  StockDetailOrdersResponse,
  StockDetailResponse,
} from "../types/stockDetail";

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
