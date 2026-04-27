import { describe, expect, it } from "vitest";
import { formatDecimal } from "../format/decimal";

describe("formatDecimal", () => {
  it("formats large integer with locale grouping", () => {
    expect(formatDecimal("117800000", "ko-KR")).toBe("117,800,000");
  });

  it("preserves fractional part", () => {
    expect(formatDecimal("0.05", "en-US", { maximumFractionDigits: 8 })).toBe(
      "0.05",
    );
  });

  it("returns input verbatim when not finite", () => {
    expect(formatDecimal("abc")).toBe("abc");
  });

  it("handles null/undefined as em dash", () => {
    expect(formatDecimal(null)).toBe("—");
    expect(formatDecimal(undefined)).toBe("—");
  });
});
