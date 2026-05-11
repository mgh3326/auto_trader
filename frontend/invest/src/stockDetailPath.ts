import type { Market } from "./types/invest";

const MARKET_ROUTE: Record<Market, "kr" | "us" | "crypto"> = {
  KR: "kr",
  US: "us",
  CRYPTO: "crypto",
};

export function stockDetailMarketParam(market: Market): "kr" | "us" | "crypto" {
  return MARKET_ROUTE[market];
}

export function stockDetailPath(market: Market, symbol: string): string | null {
  const cleanSymbol = symbol.trim();
  if (!cleanSymbol) return null;
  return `/stocks/${MARKET_ROUTE[market]}/${encodeURIComponent(cleanSymbol)}`;
}
