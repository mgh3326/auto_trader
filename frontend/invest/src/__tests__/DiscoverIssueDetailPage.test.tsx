// frontend/invest/src/__tests__/DiscoverIssueDetailPage.test.tsx
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { DiscoverIssueDetailPage, type DiscoverIssueDetailPageProps } from "../pages/DiscoverIssueDetailPage";
import type { MarketIssue, MarketIssuesResponse } from "../types/newsIssues";

function makeIssue(over: Partial<MarketIssue>): MarketIssue {
  return {
    id: "i",
    market: "kr",
    rank: 1,
    issue_title: "이슈",
    subtitle: null,
    direction: "neutral",
    source_count: 1,
    article_count: 1,
    updated_at: "2026-05-07T12:00:00Z",
    summary: null,
    related_symbols: [],
    related_sectors: [],
    articles: [],
    signals: { recency_score: 0, source_diversity_score: 0, mention_score: 0 },
    ...over,
  };
}

function makeResponse(items: MarketIssue[], over: Partial<MarketIssuesResponse> = {}): MarketIssuesResponse {
  return {
    market: "all",
    as_of: "2026-05-07T12:00:00Z",
    window_hours: 24,
    items,
    ...over,
  };
}

function renderAt(path: string, state: DiscoverIssueDetailPageProps["state"]) {
  return render(
    <MemoryRouter initialEntries={[`/invest/app${path}`]} basename="/invest/app">
      <Routes>
        <Route
          path="/discover/issues/:issueId"
          element={<DiscoverIssueDetailPage state={state} reload={() => {}} />}
        />
      </Routes>
    </MemoryRouter>,
  );
}

test("renders matched issue with impact map, related symbols and article links", () => {
  const matched = makeIssue({
    id: "abc",
    issue_title: "카카오 1분기 최대 실적",
    summary: "플랫폼 성장과 수익성 개선",
    direction: "up",
    related_sectors: ["결제거래 서비스"],
    related_symbols: [{ symbol: "035720", market: "kr", canonical_name: "카카오", mention_count: 5 }],
    articles: [{ id: 1, title: "카카오 실적 기사", url: "https://example.com/a", source: "연합", feed_source: "naver", published_at: null, summary: null, matched_terms: [] }],
  });
  renderAt("/discover/issues/abc", { status: "ready", data: makeResponse([matched]) });

  expect(screen.getByText("카카오 1분기 최대 실적")).toBeInTheDocument();
  expect(screen.getByText("플랫폼 성장과 수익성 개선")).toBeInTheDocument();
  expect(screen.getByText("결제거래 서비스")).toBeInTheDocument();
  expect(screen.getByText("카카오")).toBeInTheDocument();
  expect(screen.getByText("카카오 실적 기사")).toHaveAttribute("href", "https://example.com/a");
  expect(
    screen.getByText(/뉴스 기반 참고 정보이며 매매 추천이 아닙니다./),
  ).toBeInTheDocument();
});

test("renders not-found state when id is missing", () => {
  renderAt("/discover/issues/missing", { status: "ready", data: makeResponse([]) });
  expect(
    screen.getByText(/이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요./),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "발견으로 돌아가기" })).toHaveAttribute(
    "href",
    "/invest/app/discover",
  );
});

test("renders symbols-empty notice when item has no symbols", () => {
  const matched = makeIssue({ id: "abc", issue_title: "T", related_symbols: [] });
  renderAt("/discover/issues/abc", { status: "ready", data: makeResponse([matched]) });
  expect(screen.getByText("관련 종목 분석은 준비 중입니다.")).toBeInTheDocument();
});

test("renders loading and error states", () => {
  renderAt("/discover/issues/abc", { status: "loading" });
  expect(screen.getByText("불러오는 중…")).toBeInTheDocument();

  renderAt("/discover/issues/abc", { status: "error", message: "boom" });
  expect(screen.getByText("잠시 후 다시 시도해 주세요.")).toBeInTheDocument();
  expect(screen.getByText(/boom/)).toBeInTheDocument();
});
