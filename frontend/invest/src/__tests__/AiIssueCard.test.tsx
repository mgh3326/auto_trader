// frontend/invest/src/__tests__/AiIssueCard.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";
import { AiIssueCard } from "../components/discover/AiIssueCard";
import type { MarketIssue } from "../types/newsIssues";

const issue: MarketIssue = {
  id: "abc",
  market: "us",
  rank: 1,
  issue_title: "Fed 금리 동결 시사",
  subtitle: "정책 유지",
  direction: "up",
  source_count: 3,
  article_count: 7,
  updated_at: "2026-05-07T11:30:00Z",
  summary: null,
  related_symbols: [],
  related_sectors: ["금리"],
  articles: [],
  signals: { recency_score: 1, source_diversity_score: 0.5, mention_score: 0.8 },
};

test("renders rank, title, subtitle, counts, indicator and link", () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/app/"]}>
      <AiIssueCard issue={issue} now={new Date("2026-05-07T12:00:00Z")} />
    </MemoryRouter>,
  );

  expect(screen.getByText("1")).toBeInTheDocument();
  expect(screen.getByText("Fed 금리 동결 시사")).toBeInTheDocument();
  expect(screen.getByText(/정책 유지/)).toBeInTheDocument();
  expect(screen.getByText(/3개 출처/)).toBeInTheDocument();
  expect(screen.getByText(/기사 7개/)).toBeInTheDocument();
  expect(screen.getByText(/30분 전/)).toBeInTheDocument();
  expect(screen.getByLabelText("상승 이슈")).toBeInTheDocument();
  expect(screen.getByRole("link")).toHaveAttribute(
    "href",
    "/invest/app/discover/issues/abc",
  );
});

test("falls back to summary when subtitle is missing", () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/app/"]}>
      <AiIssueCard
        issue={{ ...issue, subtitle: null, summary: "요약 문장" }}
        now={new Date("2026-05-07T12:00:00Z")}
      />
    </MemoryRouter>,
  );
  expect(screen.getByText(/요약 문장/)).toBeInTheDocument();
});
