import { describe, expect, it } from "vitest";

import {
  computeDistancePct,
  formatDistancePct,
  groupWatchesBySymbol,
  hasMaxAction,
} from "../components/watches/watchGrouping";
import type { WatchAlertRow } from "../types/watches";

function makeRow(overrides: Partial<WatchAlertRow> = {}): WatchAlertRow {
  return {
    alert_uuid: "a1",
    source_report_uuid: "r1",
    market: "kr",
    symbol: "005930",
    symbol_name: "삼성전자",
    target_kind: "asset",
    metric: "price_below",
    operator: "below",
    threshold: "70000",
    threshold_high: null,
    status: "active",
    valid_until: "2026-09-01T00:00:00Z",
    intent: "buy_review",
    action_mode: "notify_only",
    rationale: "저점 매수",
    trigger_checklist: [],
    max_action: {},
    current_price: "72000",
    proximity_band: "within_1_pct",
    last_event: null,
    near_expiry: false,
    ...overrides,
  };
}

describe("groupWatchesBySymbol", () => {
  it("groups multiple rungs for the same symbol into one ladder, sorted by threshold", () => {
    const rows = [
      makeRow({ alert_uuid: "a1", threshold: "65000" }),
      makeRow({ alert_uuid: "a2", threshold: "60000" }),
      makeRow({ alert_uuid: "a3", threshold: "70000" }),
    ];

    const groups = groupWatchesBySymbol(rows);

    expect(groups).toHaveLength(1);
    expect(groups[0]!.symbol).toBe("005930");
    expect(groups[0]!.items.map((i) => i.alert_uuid)).toEqual(["a2", "a1", "a3"]);
  });

  it("keeps different symbols in separate groups", () => {
    const rows = [
      makeRow({ alert_uuid: "a1", symbol: "005930" }),
      makeRow({ alert_uuid: "a2", symbol: "000660" }),
    ];

    const groups = groupWatchesBySymbol(rows);
    expect(groups.map((g) => g.symbol).sort()).toEqual(["000660", "005930"]);
  });

  it("does not merge the same symbol across markets", () => {
    const rows = [
      makeRow({ alert_uuid: "a1", market: "kr", symbol: "BTC" }),
      makeRow({ alert_uuid: "a2", market: "crypto", symbol: "BTC" }),
    ];
    const groups = groupWatchesBySymbol(rows);
    expect(groups).toHaveLength(2);
  });

  it("sorts groups with an active rung before groups with none", () => {
    const rows = [
      makeRow({ alert_uuid: "a1", symbol: "ZZZZ", status: "expired" }),
      makeRow({ alert_uuid: "a2", symbol: "AAAA", status: "active" }),
    ];
    const groups = groupWatchesBySymbol(rows);
    expect(groups[0]!.symbol).toBe("AAAA");
  });

  it("backfills group-level symbolName/currentPrice from the first row that has one", () => {
    const rows = [
      makeRow({ alert_uuid: "a1", symbol_name: null, current_price: null }),
      makeRow({ alert_uuid: "a2", symbol_name: "삼성전자", current_price: "71000" }),
    ];
    const groups = groupWatchesBySymbol(rows);
    expect(groups[0]!.symbolName).toBe("삼성전자");
    expect(groups[0]!.currentPrice).toBe("71000");
  });
});

describe("computeDistancePct / formatDistancePct", () => {
  it("computes signed distance from current price to threshold", () => {
    const row = makeRow({ current_price: "100", threshold: "110" });
    expect(computeDistancePct(row)).toBeCloseTo(10, 5);
    expect(formatDistancePct(row)).toBe("+10.00%");
  });

  it("returns a negative distance when the threshold is below current price", () => {
    const row = makeRow({ current_price: "100", threshold: "90" });
    expect(formatDistancePct(row)).toBe("-10.00%");
  });

  it("returns null when current price is missing", () => {
    const row = makeRow({ current_price: null });
    expect(computeDistancePct(row)).toBeNull();
    expect(formatDistancePct(row)).toBeNull();
  });
});

describe("hasMaxAction", () => {
  it("is false for an empty max_action object", () => {
    expect(hasMaxAction(makeRow({ max_action: {} }))).toBe(false);
  });

  it("is true when max_action carries keys", () => {
    expect(hasMaxAction(makeRow({ max_action: { side: "buy", quantity: 10 } }))).toBe(true);
  });
});
