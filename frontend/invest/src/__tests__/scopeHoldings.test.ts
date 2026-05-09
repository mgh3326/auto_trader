import { describe, expect, it } from "vitest";
import {
  buildAccountFilterOptions,
  buildScopedPortfolioPanel,
  scopeGroupedToSource,
} from "../desktop/scopeHoldings";
import type { AccountPanelResponse, GroupedHolding } from "../types/invest";

const baseGroup: GroupedHolding = {
  groupId: "US:equity:USD:TSLA",
  symbol: "TSLA",
  market: "US",
  assetType: "equity",
  assetCategory: "us_stock",
  displayName: "Tesla",
  currency: "USD",
  totalQuantity: 6,
  averageCost: 200,
  costBasis: 1200,
  valueNative: 1200,
  valueKrw: 1_600_000,
  pnlKrw: 0,
  pnlRate: 0,
  priceState: "live",
  includedSources: ["kis", "toss_manual"],
  sourceBreakdown: [
    {
      holdingId: "h1",
      accountId: "kis-1",
      source: "kis",
      quantity: 4,
      averageCost: 234,
      costBasis: 936,
      valueNative: 924,
      valueKrw: 1_244_000,
      pnlKrw: -16_000,
      pnlRate: -0.012,
    },
    {
      holdingId: "h2",
      accountId: "toss-1",
      source: "toss_manual",
      quantity: 2,
      averageCost: 132,
      costBasis: 264,
      valueNative: 276,
      valueKrw: 356_000,
      pnlKrw: 16_000,
      pnlRate: 0.06,
    },
  ],
};

const upbitGroup: GroupedHolding = {
  groupId: "CRYPTO:crypto:KRW:BTC",
  symbol: "KRW-BTC",
  market: "CRYPTO",
  assetType: "crypto",
  assetCategory: "crypto",
  displayName: "비트코인",
  currency: "KRW",
  totalQuantity: 0.1,
  averageCost: 80_000_000,
  costBasis: 8_000_000,
  valueNative: 8_500_000,
  valueKrw: 8_500_000,
  pnlKrw: 500_000,
  pnlRate: 0.0625,
  priceState: "live",
  includedSources: ["upbit"],
  sourceBreakdown: [],
};

const panelResponse: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis", "upbit", "toss_manual"],
    excludedSources: [],
    totalValueKrw: 10_100_000,
    costBasisKrw: 9_200_000,
    pnlKrw: 900_000,
    pnlRate: 900_000 / 9_200_000,
  },
  accounts: [
    {
      accountId: "kis-1",
      displayName: "KIS Live",
      source: "kis",
      accountKind: "live",
      includedInHome: true,
      valueKrw: 1_244_000,
      costBasisKrw: 1_260_000,
      pnlKrw: -16_000,
      pnlRate: -16_000 / 1_260_000,
      cashBalances: { krw: 100_000, usd: 25.5 },
      buyingPower: { krw: 100_000, usd: 25.5 },
    },
    {
      accountId: "upbit-1",
      displayName: "Upbit",
      source: "upbit",
      accountKind: "live",
      includedInHome: true,
      valueKrw: 8_500_000,
      costBasisKrw: 8_000_000,
      pnlKrw: 500_000,
      pnlRate: 0.0625,
      cashBalances: { krw: 50_000 },
      buyingPower: { krw: 50_000 },
    },
  ],
  groupedHoldings: [baseGroup, upbitGroup],
  watchSymbols: [],
  sourceVisuals: [
    { source: "kis", tone: "navy", badge: "Live", displayName: "KIS" },
    { source: "upbit", tone: "green", badge: "Crypto", displayName: "Upbit" },
    { source: "toss_manual", tone: "gray", badge: "Manual", displayName: "Toss/manual" },
  ],
  meta: { warnings: [], watchlistAvailable: true },
};

describe("scopeGroupedToSource", () => {
  it("returns single-source groups unchanged when the source matches", () => {
    const single: GroupedHolding = {
      ...baseGroup,
      includedSources: ["toss_manual"],
      sourceBreakdown: [baseGroup.sourceBreakdown[1]!],
    };
    const out = scopeGroupedToSource([single], "toss_manual");
    expect(out).toHaveLength(1);
    expect(out[0]).toBe(single);
  });

  it("recomputes totals from sourceBreakdown for multi-source groups", () => {
    const out = scopeGroupedToSource([baseGroup], "kis");
    expect(out).toHaveLength(1);
    const sliced = out[0]!;
    expect(sliced.includedSources).toEqual(["kis"]);
    expect(sliced.totalQuantity).toBe(4);
    expect(sliced.costBasis).toBe(936);
    expect(sliced.valueNative).toBe(924);
    expect(sliced.valueKrw).toBe(1_244_000);
    expect(sliced.pnlKrw).toBe(-16_000);
    expect(sliced.averageCost).toBe(234);
    expect(sliced.pnlRate).toBeCloseTo((924 - 936) / 936);
    expect(sliced.sourceBreakdown).toHaveLength(1);
    expect(sliced.sourceBreakdown[0]!.source).toBe("kis");
  });

  it("omits groups whose includedSources do not contain the source", () => {
    const out = scopeGroupedToSource([baseGroup], "upbit");
    expect(out).toEqual([]);
  });

  it("skips multi-source groups with empty sourceBreakdown rather than misrepresenting them", () => {
    const orphan: GroupedHolding = {
      ...baseGroup,
      sourceBreakdown: [],
    };
    const out = scopeGroupedToSource([orphan], "kis");
    expect(out).toEqual([]);
  });
});

describe("buildScopedPortfolioPanel", () => {
  it("keeps all-account rows and home summary totals for all", () => {
    const scoped = buildScopedPortfolioPanel(panelResponse, "all");
    expect(scoped.selected.label).toBe("전체");
    expect(scoped.groupedHoldings).toBe(panelResponse.groupedHoldings);
    expect(scoped.totalValueKrw).toBe(10_100_000);
    expect(scoped.costBasisKrw).toBe(9_200_000);
    expect(scoped.pnlKrw).toBe(900_000);
    expect(scoped.cashBalances).toEqual({ krw: 150_000, usd: 25.5 });
  });

  it("builds filter options from accounts and holding-only manual sources", () => {
    const options = buildAccountFilterOptions(panelResponse);
    expect(options.map((option) => option.key)).toEqual(["all", "kis", "upbit", "toss_manual"]);
    expect(options.map((option) => option.label)).toEqual(["전체", "KIS", "Upbit", "Toss/manual"]);
    expect(options.find((option) => option.key === "toss_manual")?.cashBalances).toEqual({ krw: null, usd: null });
  });

  it("recomputes KIS totals and cash from sourceBreakdown only", () => {
    const scoped = buildScopedPortfolioPanel(panelResponse, "kis");
    expect(scoped.groupedHoldings).toHaveLength(1);
    expect(scoped.groupedHoldings[0]!.includedSources).toEqual(["kis"]);
    expect(scoped.totalValueKrw).toBe(1_244_000);
    expect(scoped.costBasisKrw).toBe(1_260_000);
    expect(scoped.pnlKrw).toBe(-16_000);
    expect(scoped.pnlRate).toBeCloseTo(-16_000 / 1_260_000);
    expect(scoped.cashBalances).toEqual({ krw: 100_000, usd: 25.5 });
  });

  it("recomputes Upbit totals independently from all-account totals", () => {
    const scoped = buildScopedPortfolioPanel(panelResponse, "upbit");
    expect(scoped.groupedHoldings.map((group) => group.symbol)).toEqual(["KRW-BTC"]);
    expect(scoped.totalValueKrw).toBe(8_500_000);
    expect(scoped.costBasisKrw).toBe(8_000_000);
    expect(scoped.pnlKrw).toBe(500_000);
    expect(scoped.pnlRate).toBeCloseTo(0.0625);
    expect(scoped.cashBalances).toEqual({ krw: 50_000, usd: null });
  });

  it("falls back to all for missing selected keys", () => {
    const scoped = buildScopedPortfolioPanel(panelResponse, "alpaca_paper");
    expect(scoped.selected.key).toBe("all");
    expect(scoped.totalValueKrw).toBe(panelResponse.homeSummary.totalValueKrw);
  });
});
