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
  // Crypto DB symbols arrive dot-format (KRW.XRP; app/core/symbol.to_db_symbol).
  // Fold "." → "-" so KRW.XRP joins the KRW- dash path instead of falling through
  // to the bare-symbol branch (which would emit KRW-KRW.XRP). Dash/bare forms
  // already normalized here are unaffected since they contain no ".".
  const clean = symbol.trim().toUpperCase().replace(/\./g, "-");
  if (!clean) return clean;
  if (clean.startsWith("KRW-")) return clean;
  if (clean.endsWith("-KRW")) return `KRW-${clean.slice(0, -4)}`;
  if (!clean.includes("-")) return `KRW-${clean}`;
  return clean;
}

export function stockDetailMarketParam(market: RouteMarket): StockDetailMarketParam {
  return routeMarketParam(market);
}

export function stockDetailRouteSymbol(market: RouteMarket, symbol: string): string {
  const cleanSymbol = symbol.trim();
  if (routeMarketParam(market) !== "crypto") return cleanSymbol;
  return normalizeCryptoRouteSymbol(cleanSymbol);
}

export function stockDetailPath(market: RouteMarket, symbol: string): string | null {
  const cleanSymbol = symbol.trim();
  if (!cleanSymbol) return null;
  const marketParam = routeMarketParam(market);
  return `/stocks/${marketParam}/${encodeURIComponent(stockDetailRouteSymbol(market, cleanSymbol))}`;
}
