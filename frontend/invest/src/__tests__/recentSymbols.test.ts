import { beforeEach, test, expect } from "vitest";
import { loadRecentSymbols, recordRecentSymbol } from "../desktop/recentSymbols";

const KEY = "invest.recentSymbols.v1";

beforeEach(() => {
  localStorage.clear();
});

test("returns empty array when nothing stored", () => {
  expect(loadRecentSymbols()).toEqual([]);
});

test("records and loads a symbol", () => {
  recordRecentSymbol({
    symbol: "005930",
    market: "kr",
    displayName: "삼성전자",
    lastViewedAt: "2026-05-09T10:00:00Z",
    source: "right-panel",
  });
  const items = loadRecentSymbols();
  expect(items).toHaveLength(1);
  expect(items[0]?.symbol).toBe("005930");
});

test("dedupes by market+symbol, keeps newest first", () => {
  recordRecentSymbol({ symbol: "A", market: "us", displayName: "A Inc", lastViewedAt: "2026-01-01T00:00:00Z" });
  recordRecentSymbol({ symbol: "B", market: "us", displayName: "B Inc", lastViewedAt: "2026-01-02T00:00:00Z" });
  recordRecentSymbol({ symbol: "A", market: "us", displayName: "A Inc", lastViewedAt: "2026-01-03T00:00:00Z" });
  const items = loadRecentSymbols();
  expect(items).toHaveLength(2);
  expect(items[0]?.symbol).toBe("A");
  expect(items[1]?.symbol).toBe("B");
});

test("handles corrupt localStorage gracefully", () => {
  localStorage.setItem(KEY, "not json");
  expect(loadRecentSymbols()).toEqual([]);
});

test("handles invalid array entries gracefully", () => {
  localStorage.setItem(KEY, JSON.stringify([{ bad: true }, { symbol: "X", market: "us", displayName: "X", lastViewedAt: "t" }]));
  const items = loadRecentSymbols();
  expect(items).toHaveLength(1);
  expect(items[0]?.symbol).toBe("X");
});
