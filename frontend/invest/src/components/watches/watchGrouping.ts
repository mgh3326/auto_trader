// Pure grouping/ladder helpers for the /invest/watches browsing page
// (INVEST-WATCH-UI §57차 item ①). Symbol-grouped so a symbol with multiple
// price-ladder watch rungs (e.g. buy-the-dip at three thresholds) renders as
// one card instead of N disconnected table rows — the existing
// WatchAlertsPanel (a flat table) intentionally keeps its own layout;
// this is the dedicated ladder view.

import type { WatchAlertRow } from "../../types/watches";

export interface WatchGroup {
  market: WatchAlertRow["market"];
  symbol: string;
  symbolName: string | null;
  currentPrice: string | null;
  items: WatchAlertRow[];
}

function ladderCompare(a: WatchAlertRow, b: WatchAlertRow): number {
  const at = Number(a.threshold);
  const bt = Number(b.threshold);
  if (Number.isFinite(at) && Number.isFinite(bt) && at !== bt) return at - bt;
  return a.alert_uuid.localeCompare(b.alert_uuid);
}

// Active groups first (they're the ones a user is watching right now), then
// alphabetical by symbol within each bucket for a stable, scannable order.
function groupCompare(a: WatchGroup, b: WatchGroup): number {
  const aActive = a.items.some((item) => item.status === "active") ? 0 : 1;
  const bActive = b.items.some((item) => item.status === "active") ? 0 : 1;
  if (aActive !== bActive) return aActive - bActive;
  return a.symbol.localeCompare(b.symbol);
}

export function groupWatchesBySymbol(items: WatchAlertRow[]): WatchGroup[] {
  const byKey = new Map<string, WatchGroup>();

  for (const item of items) {
    const key = `${item.market}:${item.symbol}`;
    let group = byKey.get(key);
    if (!group) {
      group = {
        market: item.market,
        symbol: item.symbol,
        symbolName: item.symbol_name,
        currentPrice: item.current_price,
        items: [],
      };
      byKey.set(key, group);
    }
    group.items.push(item);
    if (!group.symbolName && item.symbol_name) group.symbolName = item.symbol_name;
    if (!group.currentPrice && item.current_price) group.currentPrice = item.current_price;
  }

  const groups = Array.from(byKey.values());
  for (const group of groups) group.items.sort(ladderCompare);
  groups.sort(groupCompare);
  return groups;
}

// Signed distance from the current price to this rung's threshold, as a
// percentage of the current price. Positive = threshold is above current
// price (room to run before triggering an "above" watch); negative = below.
export function computeDistancePct(row: WatchAlertRow): number | null {
  const price = row.current_price != null ? Number(row.current_price) : null;
  const threshold = row.threshold != null ? Number(row.threshold) : null;
  if (
    price == null ||
    threshold == null ||
    !Number.isFinite(price) ||
    !Number.isFinite(threshold) ||
    price === 0
  ) {
    return null;
  }
  return ((threshold - price) / price) * 100;
}

export function formatDistancePct(row: WatchAlertRow): string | null {
  const pct = computeDistancePct(row);
  if (pct == null) return null;
  const sign = pct > 0 ? "+" : "";
  return `${sign}${pct.toFixed(2)}%`;
}

export function hasMaxAction(row: WatchAlertRow): boolean {
  return Boolean(row.max_action) && Object.keys(row.max_action).length > 0;
}
