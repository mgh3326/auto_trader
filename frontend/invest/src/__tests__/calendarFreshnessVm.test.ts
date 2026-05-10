import { describe, expect, test } from "vitest";
import { dataStateLabel, freshnessBadgeLabel } from "../components/calendar/vm";

describe("calendar freshness VM helpers", () => {
  test("dataStateLabel covers every state", () => {
    expect(dataStateLabel("loaded")).toBe("최신");
    expect(dataStateLabel("empty")).toBe("일정 없음");
    expect(dataStateLabel("partial")).toBe("일부 수집 중");
    expect(dataStateLabel("missing")).toBe("미수집");
    expect(dataStateLabel("error")).toBe("수집 실패");
    expect(dataStateLabel("stale")).toBe("오래된 데이터");
  });

  test("freshnessBadgeLabel formats by source + state", () => {
    expect(
      freshnessBadgeLabel({
        source: "finnhub",
        category: "earnings",
        market: "us",
        state: "fresh",
        succeededPartitions: 5,
        failedPartitions: 0,
        missingPartitions: 0,
        eventCount: 23,
      }),
    ).toBe("Finnhub 실적 · 최신");

    expect(
      freshnessBadgeLabel({
        source: "dart",
        category: "disclosure",
        market: "kr",
        state: "failed",
        succeededPartitions: 1,
        failedPartitions: 2,
        missingPartitions: 0,
        eventCount: 0,
      }),
    ).toBe("DART 공시 · 수집 실패");
  });
});
