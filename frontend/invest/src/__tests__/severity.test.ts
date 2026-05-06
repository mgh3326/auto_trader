// frontend/invest/src/__tests__/severity.test.ts
import { expect, test } from "vitest";
import {
  countByRiskCategory,
  describeSeverity,
  sortIssueItems,
} from "../components/discover/severity";
import type { NewsRadarItem } from "../types/newsRadar";

function makeItem(overrides: Partial<NewsRadarItem>): NewsRadarItem {
  return {
    id: "x",
    title: "t",
    source: null,
    feed_source: null,
    url: "",
    published_at: null,
    market: "all",
    risk_category: null,
    severity: "low",
    themes: [],
    symbols: [],
    included_in_briefing: false,
    briefing_reason: null,
    briefing_score: 0,
    snippet: null,
    matched_terms: [],
    ...overrides,
  };
}

test("describeSeverity maps to indicator label", () => {
  expect(describeSeverity("high").label).toBe("강한 이슈");
  expect(describeSeverity("medium").label).toBe("관심 이슈");
  expect(describeSeverity("low").label).toBe("참고");
});

test("countByRiskCategory groups items by risk_category", () => {
  const items = [
    makeItem({ id: "1", risk_category: "macro_policy" }),
    makeItem({ id: "2", risk_category: "macro_policy" }),
    makeItem({ id: "3", risk_category: "geopolitical_oil" }),
    makeItem({ id: "4", risk_category: null }),
  ];
  const counts = countByRiskCategory(items);
  expect(counts.macro_policy).toBe(2);
  expect(counts.geopolitical_oil).toBe(1);
  expect(counts.uncategorized).toBe(1);
});

test("sortIssueItems orders by severity then briefing_score then published_at", () => {
  const items = [
    makeItem({ id: "a", severity: "low", briefing_score: 10, published_at: "2026-05-07T10:00:00Z" }),
    makeItem({ id: "b", severity: "high", briefing_score: 5, published_at: "2026-05-07T08:00:00Z" }),
    makeItem({ id: "c", severity: "high", briefing_score: 9, published_at: "2026-05-07T08:00:00Z" }),
    makeItem({ id: "d", severity: "medium", briefing_score: 0, published_at: "2026-05-07T11:00:00Z" }),
  ];
  expect(sortIssueItems(items).map((i) => i.id)).toEqual(["c", "b", "d", "a"]);
});
