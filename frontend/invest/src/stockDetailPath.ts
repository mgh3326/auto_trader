import type { Market } from "./types/invest";

type RouteMarket = Market | "kr" | "us" | "crypto";
type StockDetailMarketParam = "kr" | "us" | "crypto";

const MARKET_ROUTE: Record<Market, StockDetailMarketParam> = {
  KR: "kr",
  US: "us",
  CRYPTO: "crypto",
};

function routeMarketParam(market: RouteMarket): StockDetailMarketParam {
  if (market === "KR" || market === "US" || market === "CRYPTO") {
    return MARKET_ROUTE[market];
  }
  return market;
}

function normalizeCryptoRouteSymbol(symbol: string): string {
  const clean = symbol.trim().toUpperCase();
  if (!clean) return clean;
  if (clean.startsWith("KRW-")) return clean;
  if (clean.endsWith("-KRW")) return `KRW-${clean.slice(0, -4)}`;
  if (!clean.includes("-")) return `KRW-${clean}`;
  return clean;
}

export function stockDetailMarketParam(market: Market): StockDetailMarketParam {
  return MARKET_ROUTE[market];
}

export function stockDetailRouteSymbol(market: RouteMarket, symbol: string): string {
  const cleanSymbol = symbol.trim();
  if (routeMarketParam(market) !== "crypto") return cleanSymbol;
  return normalizeCryptoRouteSymbol(cleanSymbol);
}

export function stockDetailPath(market: Market, symbol: string): string | null {
  const cleanSymbol = symbol.trim();
  if (!cleanSymbol) return null;
  const marketParam = MARKET_ROUTE[market];
  return `/stocks/${marketParam}/${encodeURIComponent(stockDetailRouteSymbol(market, cleanSymbol))}`;
}
