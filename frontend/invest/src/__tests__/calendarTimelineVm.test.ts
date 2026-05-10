import { describe, expect, test } from "vitest";
import {
  dayHeaderLabel,
  dayTotalLabel,
  dayEmptyLabel,
  monthEmptyLabel,
  monthDaysIso,
  sourceFriendlyLabel,
  sourceStaleStatusCopy,
} from "../components/calendar/vm";
import type { CalendarSourceStatus } from "../types/calendar";

describe("ROB-185 timeline VM helpers", () => {
  test("monthDaysIso returns every in-month date in order (May 2026 -> 31 entries)", () => {
    const out = monthDaysIso(new Date(2026, 4, 1));
    expect(out).toHaveLength(31);
    expect(out[0]).toBe("2026-05-01");
    expect(out[30]).toBe("2026-05-31");
  });

  test("monthDaysIso handles 30-day months (June)", () => {
    const out = monthDaysIso(new Date(2026, 5, 15));
    expect(out).toHaveLength(30);
    expect(out[0]).toBe("2026-06-01");
    expect(out[29]).toBe("2026-06-30");
  });

  test("monthDaysIso handles leap-year February", () => {
    const out = monthDaysIso(new Date(2024, 1, 10));
    expect(out).toHaveLength(29);
    expect(out[28]).toBe("2024-02-29");
  });

  test("dayHeaderLabel prefixes 오늘 / 내일 within the current month", () => {
    expect(dayHeaderLabel("2026-05-11", "2026-05-11")).toBe("오늘 · 5월 11일 (월)");
    expect(dayHeaderLabel("2026-05-12", "2026-05-11")).toBe("내일 · 5월 12일 (화)");
    expect(dayHeaderLabel("2026-05-15", "2026-05-11")).toBe("5월 15일 (금)");
  });

  test("dayTotalLabel renders Korean noun phrasing", () => {
    expect(dayTotalLabel(3)).toBe("일정 3개");
    expect(dayTotalLabel(0)).toBe("");
  });

  test("dayEmptyLabel + monthEmptyLabel are fixed Korean copy", () => {
    expect(dayEmptyLabel()).toBe("이 날은 예정된 일정이 없어요");
    expect(monthEmptyLabel()).toBe("이번 달은 예정된 주요 일정이 없어요");
  });

  test("sourceFriendlyLabel maps internal source ids to plain Korean", () => {
    expect(sourceFriendlyLabel("finnhub")).toBe("미국 실적 일정");
    expect(sourceFriendlyLabel("dart")).toBe("한국 공시");
    expect(sourceFriendlyLabel("forexfactory")).toBe("경제 지표");
    // Unknown source falls back to a generic label, NOT the raw id.
    expect(sourceFriendlyLabel("wisefn")).toBe("기타 일정");
  });

  test("sourceStaleStatusCopy emits Toss-friendly copy for non-fresh states", () => {
    const stale: CalendarSourceStatus = {
      source: "finnhub", category: "earnings", market: "us", state: "stale",
      lastSuccessAt: null, lastFailureAt: null, lastError: null,
      succeededPartitions: 0, failedPartitions: 0, missingPartitions: 0, eventCount: 0,
    };
    expect(sourceStaleStatusCopy(stale)).toBe("방금 업데이트되지 않았어요");
    expect(sourceStaleStatusCopy({ ...stale, state: "failed" })).toBe("잠시 후 다시 확인할게요");
    expect(sourceStaleStatusCopy({ ...stale, state: "missing" })).toBe("잠시 후 다시 확인할게요");
    expect(sourceStaleStatusCopy({ ...stale, state: "fresh" })).toBeNull();
  });
});
