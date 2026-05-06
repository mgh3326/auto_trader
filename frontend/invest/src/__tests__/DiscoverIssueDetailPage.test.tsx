// frontend/invest/src/__tests__/DiscoverIssueDetailPage.test.tsx
import type { ComponentProps } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { expect, test } from "vitest";
import { DiscoverIssueDetailPage } from "../pages/DiscoverIssueDetailPage";
import type { NewsRadarItem, NewsRadarResponse } from "../types/newsRadar";

function makeItem(over: Partial<NewsRadarItem>): NewsRadarItem {
  return {
    id: "i",
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
    ...over,
  };
}

function response(items: NewsRadarItem[]): NewsRadarResponse {
  return {
    market: "all",
    as_of: "2026-05-07T12:00:00Z",
    readiness: {
      status: "ready",
      latest_scraped_at: null,
      latest_published_at: null,
      recent_6h_count: 0,
      recent_24h_count: 0,
      source_count: 0,
      stale: false,
      max_age_minutes: 0,
      warnings: [],
    },
    summary: {
      high_risk_count: 0,
      total_count: items.length,
      included_in_briefing_count: 0,
      excluded_but_collected_count: 0,
    },
    sections: [],
    items,
    excluded_items: [],
    source_coverage: [],
  };
}

function renderAt(path: string, state: ComponentProps<typeof DiscoverIssueDetailPage>["state"]) {
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

test("renders matched issue with impact map and related symbols", () => {
  const matched = makeItem({
    id: "abc",
    title: "Fed 금리",
    snippet: "정책 유지",
    risk_category: "macro_policy",
    symbols: ["SPY"],
    severity: "high",
    published_at: "2026-05-07T11:30:00Z",
    source: "Reuters",
  });
  renderAt("/discover/issues/abc", { status: "ready", data: response([matched]) });

  expect(screen.getByText("Fed 금리")).toBeInTheDocument();
  expect(screen.getByText("정책 유지")).toBeInTheDocument();
  expect(screen.getByText("금리 민감 성장주")).toBeInTheDocument();
  expect(screen.getByText("SPY")).toBeInTheDocument();
  expect(
    screen.getByText(/뉴스 기반 참고 정보이며 매매 추천이 아닙니다./),
  ).toBeInTheDocument();
});

test("renders not-found state when id is missing", () => {
  renderAt("/discover/issues/missing", { status: "ready", data: response([]) });
  expect(
    screen.getByText(/이슈를 찾을 수 없습니다. 시간이 지나 목록에서 빠졌을 수 있어요./),
  ).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "발견으로 돌아가기" })).toHaveAttribute(
    "href",
    "/invest/app/discover",
  );
});

test("renders symbols-empty notice when item has no symbols", () => {
  const matched = makeItem({ id: "abc", title: "T", symbols: [] });
  renderAt("/discover/issues/abc", { status: "ready", data: response([matched]) });
  expect(screen.getByText("관련 종목 분석은 준비 중입니다.")).toBeInTheDocument();
});

test("renders loading and error states", () => {
  renderAt("/discover/issues/abc", { status: "loading" });
  expect(screen.getByText("불러오는 중…")).toBeInTheDocument();

  // re-render with error
  renderAt("/discover/issues/abc", { status: "error", message: "boom" });
  expect(screen.getByText("잠시 후 다시 시도해 주세요.")).toBeInTheDocument();
  expect(screen.getByText(/boom/)).toBeInTheDocument();
});
