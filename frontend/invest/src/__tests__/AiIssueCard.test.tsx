// frontend/invest/src/__tests__/AiIssueCard.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import type { NewsRadarItem } from "../types/newsRadar";

const item: NewsRadarItem = {
  id: "abc",
  title: "Fed 금리 동결 시사",
  source: "Reuters",
  feed_source: "reuters",
  url: "https://example.com/news/1",
  published_at: "2026-05-07T11:30:00Z",
  market: "us",
  risk_category: "macro_policy",
  severity: "high",
  themes: ["rates"],
  symbols: ["SPY"],
  included_in_briefing: true,
  briefing_reason: null,
  briefing_score: 80,
  snippet: "위원회는 현재 정책 유지를 시사",
  matched_terms: ["fomc"],
};

test("renders rank, title, snippet, related news count, indicator and link", () => {
  render(
    <MemoryRouter basename="/invest/app" initialEntries={["/invest/app/"]}>
      <AiIssueCard
        rank={1}
        item={item}
        relatedCount={3}
        now={new Date("2026-05-07T12:00:00Z")}
      />
    </MemoryRouter>,
  );

  expect(screen.getByText("1")).toBeInTheDocument();
  expect(screen.getByText("Fed 금리 동결 시사")).toBeInTheDocument();
  expect(screen.getByText(/위원회는 현재 정책 유지를 시사/)).toBeInTheDocument();
  expect(screen.getByText(/관련 뉴스 3개/)).toBeInTheDocument();
  expect(screen.getByText(/30분 전/)).toBeInTheDocument();
  expect(screen.getByLabelText("강한 이슈")).toBeInTheDocument();
  expect(screen.getByRole("link")).toHaveAttribute(
    "href",
    "/invest/app/discover/issues/abc",
  );
});

test("falls back to themes when snippet is missing", () => {
  render(
    <MemoryRouter basename="/invest/app" initialEntries={["/invest/app/"]}>
      <AiIssueCard
        rank={2}
        item={{ ...item, snippet: null, themes: ["fomc", "rates"] }}
        relatedCount={1}
        now={new Date("2026-05-07T12:00:00Z")}
      />
    </MemoryRouter>,
  );
  expect(screen.getByText(/fomc, rates/)).toBeInTheDocument();
});
