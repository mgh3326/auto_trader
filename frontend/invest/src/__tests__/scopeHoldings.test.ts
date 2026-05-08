import { describe, expect, it } from "vitest";
import { scopeGroupedToSource } from "../desktop/scopeHoldings";
import type { GroupedHolding } from "../types/invest";

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
    expect(sliced.pnlRate).toBeCloseTo(-16_000 / 936);
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
