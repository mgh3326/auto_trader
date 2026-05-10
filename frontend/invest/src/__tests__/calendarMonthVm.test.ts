import { describe, expect, test } from "vitest";
import {
  addMonths,
  endOfMonth,
  fmtLocal,
  gridEndFromMonth,
  gridStartFromMonth,
  monthLabel,
  monthTitleLabel,
  selectedDateLabel,
  startOfMonth,
  weekStartOf,
} from "../components/calendar/vm";

describe("ROB-165 month/grid helpers", () => {
  test("startOfMonth returns 1st of month at local midnight", () => {
    expect(fmtLocal(startOfMonth(new Date(2026, 4, 13, 11)))).toBe("2026-05-01");
    expect(fmtLocal(startOfMonth(new Date(2026, 1, 28)))).toBe("2026-02-01");
  });

  test("endOfMonth handles 28/30/31-day months and leap year", () => {
    expect(fmtLocal(endOfMonth(new Date(2026, 4, 13)))).toBe("2026-05-31");
    expect(fmtLocal(endOfMonth(new Date(2026, 3, 1)))).toBe("2026-04-30");
    expect(fmtLocal(endOfMonth(new Date(2026, 1, 1)))).toBe("2026-02-28");
    expect(fmtLocal(endOfMonth(new Date(2024, 1, 1)))).toBe("2024-02-29");
  });

  test("addMonths avoids end-of-month overflow", () => {
    // Jan 31 + 1m must give Feb 1, not Mar 3.
    expect(fmtLocal(addMonths(new Date(2026, 0, 31), 1))).toBe("2026-02-01");
    expect(fmtLocal(addMonths(new Date(2026, 4, 15), -1))).toBe("2026-04-01");
    expect(fmtLocal(addMonths(new Date(2026, 4, 15), 12))).toBe("2027-05-01");
  });

  test("gridStartFromMonth aligns to the Sunday on/before the 1st", () => {
    // 2026-05-01 is Friday -> grid starts Sun 2026-04-26.
    expect(fmtLocal(gridStartFromMonth(new Date(2026, 4, 1)))).toBe("2026-04-26");
    // 2026-03-01 is Sunday -> grid starts that day.
    expect(fmtLocal(gridStartFromMonth(new Date(2026, 2, 1)))).toBe("2026-03-01");
  });

  test("gridEndFromMonth is gridStart + 41 days (6 weeks)", () => {
    expect(fmtLocal(gridEndFromMonth(new Date(2026, 4, 1)))).toBe("2026-06-06");
    expect(fmtLocal(gridEndFromMonth(new Date(2026, 2, 1)))).toBe("2026-04-11");
  });

  test("weekStartOf returns Monday-aligned date string", () => {
    expect(weekStartOf("2026-05-13")).toBe("2026-05-11"); // Wed -> Mon
    expect(weekStartOf("2026-05-11")).toBe("2026-05-11"); // Mon stays
    expect(weekStartOf("2026-05-10")).toBe("2026-05-04"); // Sun -> previous Mon
  });

  test("monthTitleLabel and monthLabel produce Korean labels", () => {
    expect(monthTitleLabel("2026-05-01")).toBe("2026년 5월");
    expect(monthLabel("2026-05-01")).toBe("5월 금융 캘린더");
  });

  test("selectedDateLabel produces Korean weekday label", () => {
    // 2026-05-13 is Wednesday.
    expect(selectedDateLabel("2026-05-13")).toBe("5월 13일 수요일 일정");
  });
});
