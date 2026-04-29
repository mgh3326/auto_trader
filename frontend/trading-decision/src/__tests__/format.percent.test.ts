import { describe, expect, it } from "vitest";
import { formatPercent } from "../format/percent";

describe("formatPercent", () => {
  it("returns em-dash on null/undefined", () => {
    expect(formatPercent(null)).toBe("—");
    expect(formatPercent(undefined)).toBe("—");
    expect(formatPercent("")).toBe("—");
  });

  it("formats positive and negative numbers with sign", () => {
    expect(formatPercent("0.5")).toBe("+0.50%");
    expect(formatPercent("-2.857")).toBe("-2.86%");
    expect(formatPercent(0)).toBe("0.00%");
  });

  it("falls back to raw string when not finite", () => {
    expect(formatPercent("not-a-number")).toBe("not-a-number");
  });

  it("respects fractionDigits override", () => {
    expect(formatPercent("1.23456", 4)).toBe("+1.2346%");
  });
});
