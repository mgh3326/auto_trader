import { describe, expect, test } from "vitest";
import {
  clampSelectedDateToMonth,
  formatKstTime,
  relativeDayPrefix,
  selectedDateLabelWithRelative,
} from "../components/calendar/vm";

describe("ROB-166 KST + relative-date helpers", () => {
  test("formatKstTime returns the backend string when present", () => {
    expect(formatKstTime("오후 9시 발표 예정")).toBe("오후 9시 발표 예정");
    expect(formatKstTime("  오전 8시  ")).toBe("오전 8시");
  });

  test("formatKstTime falls back to a single stable placeholder when null/empty", () => {
    expect(formatKstTime(null)).toBe("발표 예정 · KST");
    expect(formatKstTime(undefined)).toBe("발표 예정 · KST");
    expect(formatKstTime("")).toBe("발표 예정 · KST");
    expect(formatKstTime("   ")).toBe("발표 예정 · KST");
  });

  test("relativeDayPrefix names today/tomorrow, otherwise null", () => {
    expect(relativeDayPrefix("2026-05-11", "2026-05-11")).toBe("오늘");
    expect(relativeDayPrefix("2026-05-12", "2026-05-11")).toBe("내일");
    expect(relativeDayPrefix("2026-05-13", "2026-05-11")).toBeNull();
    // crosses month boundary
    expect(relativeDayPrefix("2026-06-01", "2026-05-31")).toBe("내일");
  });

  test("selectedDateLabelWithRelative prepends 오늘/내일 when applicable, keeps suffix", () => {
    expect(selectedDateLabelWithRelative("2026-05-11", "2026-05-11")).toBe(
      "오늘 · 5월 11일 월요일 일정",
    );
    expect(selectedDateLabelWithRelative("2026-05-12", "2026-05-11")).toBe(
      "내일 · 5월 12일 화요일 일정",
    );
    // The bare-suffix /5월 13일 수요일 일정/ regex from ROB-165 must still match.
    expect(selectedDateLabelWithRelative("2026-05-13", "2026-05-11")).toMatch(
      /5월 13일 수요일 일정/,
    );
    // Same date as today's date but ROB-165 default monthFirst case (still "오늘").
    expect(selectedDateLabelWithRelative("2026-05-01", "2026-05-01")).toBe(
      "오늘 · 5월 1일 금요일 일정",
    );
  });

  test("clampSelectedDateToMonth keeps in-range, snaps out-of-range to month-first", () => {
    const may = new Date(2026, 4, 1); // May 2026
    expect(clampSelectedDateToMonth("2026-05-13", may)).toBe("2026-05-13");
    expect(clampSelectedDateToMonth("2026-04-30", may)).toBe("2026-05-01");
    expect(clampSelectedDateToMonth("2026-06-01", may)).toBe("2026-05-01");
  });
});
