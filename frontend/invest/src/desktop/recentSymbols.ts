const KEY = "invest.recentSymbols.v1";
const MAX = 30;

export interface RecentInvestSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
  lastViewedAt: string;
  source?: "right-panel" | "signals" | "discover" | "feed-news" | "screener";
}

function isValidEntry(item: unknown): item is RecentInvestSymbol {
  if (typeof item !== "object" || item === null) return false;
  const o = item as Record<string, unknown>;
  return (
    typeof o["symbol"] === "string" &&
    typeof o["market"] === "string" &&
    typeof o["displayName"] === "string" &&
    typeof o["lastViewedAt"] === "string"
  );
}

export function loadRecentSymbols(): RecentInvestSymbol[] {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return (parsed as unknown[]).filter(isValidEntry);
  } catch {
    return [];
  }
}

export function recordRecentSymbol(sym: RecentInvestSymbol): void {
  try {
    const existing = loadRecentSymbols().filter(
      (r) => !(r.symbol === sym.symbol && r.market === sym.market),
    );
    const next = [sym, ...existing].slice(0, MAX);
    localStorage.setItem(KEY, JSON.stringify(next));
  } catch {
    // localStorage unavailable or full — ignore silently
  }
}
