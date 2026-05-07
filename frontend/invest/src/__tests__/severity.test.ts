// frontend/invest/src/__tests__/severity.test.ts
import { expect, test } from "vitest";
import { describeDirection, sortMarketIssues } from "../components/discover/severity";
import type { MarketIssue } from "../types/newsIssues";

function makeIssue(overrides: Partial<MarketIssue>): MarketIssue {
  return {
    id: "x",
    market: "kr",
    rank: 1,
    issue_title: "t",
    subtitle: null,
    direction: "neutral",
    source_count: 1,
    article_count: 1,
    updated_at: "2026-05-07T10:00:00Z",
    summary: null,
    related_symbols: [],
    related_sectors: [],
    articles: [],
    signals: { recency_score: 0, source_diversity_score: 0, mention_score: 0 },
    ...overrides,
  };
}

test("describeDirection maps to indicator label", () => {
  expect(describeDirection("up").label).toBe("상승 이슈");
  expect(describeDirection("down").label).toBe("하락 이슈");
  expect(describeDirection("mixed").label).toBe("혼조 이슈");
  expect(describeDirection("neutral").label).toBe("중립 이슈");
});

test("sortMarketIssues orders by rank then scores then updated_at", () => {
  const items = [
    makeIssue({ id: "a", rank: 3 }),
    makeIssue({ id: "b", rank: 1, signals: { recency_score: 0, source_diversity_score: 0, mention_score: 0 } }),
    makeIssue({ id: "c", rank: 1, signals: { recency_score: 1, source_diversity_score: 1, mention_score: 1 } }),
    makeIssue({ id: "d", rank: 2, updated_at: "2026-05-07T12:00:00Z" }),
  ];
  expect(sortMarketIssues(items).map((i) => i.id)).toEqual(["c", "b", "d", "a"]);
});
