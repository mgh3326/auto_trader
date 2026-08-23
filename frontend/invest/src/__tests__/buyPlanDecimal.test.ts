// verify-r1 B5 — the board must render the exact amount the API sent.
import { describe, expect, it } from "vitest";

import {
  compareDecimalStrings,
  formatDecimalString,
  parseDecimalString,
  roundDecimal,
} from "../components/buyPlan/decimal";
import { money, pct, price, quantity } from "../components/buyPlan/format";

describe("formatDecimalString", () => {
  it("keeps integers beyond Number.MAX_SAFE_INTEGER exact", () => {
    // The measured regression: Number("9007199254740993") === 9007199254740992.
    expect(Number("9007199254740993")).toBe(9007199254740992);
    expect(formatDecimalString("9007199254740993")).toBe("9,007,199,254,740,993");
  });

  it("does not lose digits on a very long amount", () => {
    const raw = "123456789012345678901234567890";
    expect(formatDecimalString(raw)).toBe("123,456,789,012,345,678,901,234,567,890");
  });

  it("groups small values without a separator", () => {
    expect(formatDecimalString("0")).toBe("0");
    expect(formatDecimalString("999")).toBe("999");
    expect(formatDecimalString("1000")).toBe("1,000");
  });

  it("rounds half-up on magnitude and carries", () => {
    expect(formatDecimalString("0.5")).toBe("1");
    expect(formatDecimalString("0.4")).toBe("0");
    expect(formatDecimalString("999.5")).toBe("1,000");
    expect(formatDecimalString("-0.5")).toBe("-1");
    expect(formatDecimalString("1.005", { maxFractionDigits: 2 })).toBe("1.01");
  });

  it("never renders a signed zero", () => {
    expect(formatDecimalString("-0.004", { maxFractionDigits: 2 })).toBe("0.00");
    expect(formatDecimalString("-0")).toBe("0");
  });

  it("returns null for input it cannot parse, so callers show a placeholder", () => {
    for (const bad of ["", "   ", "abc", "1e5", "1.2.3", null, undefined]) {
      expect(formatDecimalString(bad)).toBeNull();
    }
  });

  it("parses sign, leading zeros and bare fractions", () => {
    expect(parseDecimalString("-007.50")).toEqual({
      negative: true,
      int: "7",
      frac: "50",
    });
    expect(parseDecimalString(".5")).toEqual({ negative: false, int: "0", frac: "5" });
  });

  it("pads the fraction when fewer digits arrived than requested", () => {
    expect(roundDecimal({ negative: false, int: "1", frac: "5" }, 3)).toEqual({
      negative: false,
      int: "1",
      frac: "500",
    });
  });
});

describe("board formatters", () => {
  it("renders exact KRW past the float boundary", () => {
    expect(money("9007199254740993", "KRW")).toBe("₩9,007,199,254,740,993");
  });

  it("keeps USD cents and KRW whole", () => {
    expect(money("1234.567", "USD")).toBe("$1,234.57");
    expect(money("1234.5", "KRW")).toBe("₩1,235");
  });

  it("keeps sub-won precision on cheap coins but trims clean prices", () => {
    expect(price("0.42", "KRW")).toBe("₩0.42");
    expect(price("1000", "KRW")).toBe("₩1,000");
  });

  it("signs percentages and drops the sign at zero", () => {
    expect(pct("5")).toBe("+5.00%");
    expect(pct("-7.143", 1)).toBe("-7.1%");
    expect(pct("0")).toBe("0.00%");
  });

  it("renders a placeholder instead of zero for missing values", () => {
    expect(money(null, "KRW")).toBe("-");
    expect(price(undefined, "USD")).toBe("-");
    expect(quantity(null)).toBe("-");
    expect(pct(null)).toBe("-");
  });

  it("keeps long quantities exact", () => {
    expect(quantity("0.00000001")).toBe("0.00000001");
    expect(quantity("12345678901234567890.5")).toBe("12,345,678,901,234,567,890.5");
  });
});

describe("compareDecimalStrings", () => {
  it("orders past the float boundary", () => {
    expect(compareDecimalStrings("9007199254740993", "9007199254740992")).toBe(1);
    expect(compareDecimalStrings("-5", "3")).toBe(-1);
    expect(compareDecimalStrings("1.10", "1.1")).toBe(0);
  });

  it("returns null when either side is unparseable", () => {
    expect(compareDecimalStrings("abc", "1")).toBeNull();
  });
});
