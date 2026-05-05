import { describe, expect, it } from "vitest";
import { formatDateTime } from "../format/datetime";

describe("formatDateTime", () => {
  it("returns dash for null/undefined", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined)).toBe("—");
  });

  it("returns the original string when not a valid date", () => {
    expect(formatDateTime("not-a-date")).toBe("not-a-date");
  });

  it("formats ISO timestamps in ko-KR by default", () => {
    const result = formatDateTime("2026-05-05T10:30:00Z");
    expect(result).toMatch(/2026/);
    expect(result.length).toBeGreaterThan(0);
    // Korean default produces something like "2026. 5. 5. 오후 7:30"
    // Match a Korean month/day separator or AM/PM marker.
    expect(result).toMatch(/\.|오전|오후/);
  });
});
