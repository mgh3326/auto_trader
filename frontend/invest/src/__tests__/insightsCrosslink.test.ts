import { describe, expect, test } from "vitest";
import {
  crosslinkAnchorSlug,
  crosslinkKey,
  forecastMarket,
  retroMarket,
} from "../insightsCrosslink";

describe("forecastMarket", () => {
  test("maps known instrument_type values", () => {
    expect(forecastMarket("equity_kr")).toBe("kr");
    expect(forecastMarket("equity_us")).toBe("us");
    expect(forecastMarket("crypto")).toBe("crypto");
  });

  test("returns null for unknown/missing instrument_type", () => {
    expect(forecastMarket(null)).toBeNull();
    expect(forecastMarket("bond")).toBeNull();
  });
});

describe("retroMarket", () => {
  test("prefers the explicit market column", () => {
    expect(retroMarket("kr", null)).toBe("kr");
    expect(retroMarket("kr", "equity_us")).toBe("kr");
  });

  test("falls back to instrument_type when market is missing", () => {
    expect(retroMarket(null, "equity_us")).toBe("us");
  });

  test("returns null when neither side resolves", () => {
    expect(retroMarket(null, null)).toBeNull();
    expect(retroMarket("all", null)).toBeNull();
  });
});

describe("crosslinkKey", () => {
  test("folds equity symbols: upper + separators -> .", () => {
    expect(crosslinkKey(forecastMarket("equity_us"), "BRK-B")).toBe("us:BRK.B");
    expect(crosslinkKey(forecastMarket("equity_us"), "brk.b")).toBe("us:BRK.B");
    expect(crosslinkKey("us", "BRK/B")).toBe("us:BRK.B");
  });

  test("folds crypto symbols via stockDetailRouteSymbol", () => {
    expect(crosslinkKey("crypto", "btc")).toBe("crypto:KRW-BTC");
    expect(crosslinkKey("crypto", "KRW-BTC")).toBe("crypto:KRW-BTC");
  });

  test("kr keeps numeric codes unchanged", () => {
    expect(crosslinkKey("kr", "000660")).toBe("kr:000660");
  });

  test("returns null when market is null or symbol is empty", () => {
    expect(crosslinkKey(null, "X")).toBeNull();
    expect(crosslinkKey("kr", "  ")).toBeNull();
  });
});

describe("crosslinkAnchorSlug", () => {
  test("folds non-alphanumeric characters to '-'", () => {
    expect(crosslinkAnchorSlug("us:BRK.B")).toBe("us-BRK-B");
    expect(crosslinkAnchorSlug("crypto:KRW-BTC")).toBe("crypto-KRW-BTC");
    expect(crosslinkAnchorSlug("kr:000660")).toBe("kr-000660");
  });
});
