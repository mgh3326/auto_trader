// frontend/invest/src/__tests__/DiscoverPage.test.tsx
import type { ReactNode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { DiscoverPage } from "../pages/DiscoverPage";
import type { MarketIssue, MarketIssuesResponse } from "../types/newsIssues";

const calendarStub = {
  headline: null,
  week_label: "5월 1주차",
  from_date: "2026-05-04",
  to_date: "2026-05-10",
  today: "2026-05-07",
  tab: "all",
  days: [],
};

const fetchMock = vi.fn();
beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, json: async () => calendarStub });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

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

function renderWith(node: ReactNode) {
  return render(<MemoryRouter basename="/invest/app" initialEntries={["/invest/app/discover"]}>{node}</MemoryRouter>);
}

test("renders ranked issue cards from news-issues response", async () => {
  const items = [
    makeIssue({ id: "a", rank: 2, issue_title: "반도체 슈퍼사이클", direction: "up", source_count: 16, article_count: 20 }),
    makeIssue({ id: "b", rank: 1, issue_title: "카카오 1분기 최대 실적", direction: "up", source_count: 15, article_count: 18 }),
  ];
  renderWith(
    <DiscoverPage state={{ status: "ready", data: makeResponse(items) }} reload={() => {}} today="2026-05-07" />,
  );

  await screen.findByText("5월 1주차");
  const titles = screen.getAllByRole("link").map((a) => a.textContent ?? "");
  expect(titles[0]).toContain("카카오 1분기 최대 실적");
  expect(titles[1]).toContain("반도체 슈퍼사이클");
  expect(screen.getByText(/15개 출처/)).toBeInTheDocument();
  expect(screen.getByText(/기사 18개/)).toBeInTheDocument();
});

test("renders loading state", async () => {
  renderWith(<DiscoverPage state={{ status: "loading" }} reload={() => {}} />);
  expect(screen.getByText("AI 실시간 이슈를 불러오는 중…")).toBeInTheDocument();
  expect(screen.getByText("오늘의 주요 이벤트")).toBeInTheDocument();
  await screen.findByText("5월 1주차");
});

test("renders error state with retry", async () => {
  const reload = vi.fn();
  renderWith(<DiscoverPage state={{ status: "error", message: "boom" }} reload={reload} />);
  expect(screen.getByText("AI 실시간 이슈를 잠시 후 다시 시도해 주세요.")).toBeInTheDocument();
  expect(screen.getByText("오늘의 주요 이벤트")).toBeInTheDocument();
  expect(screen.getByText(/boom/)).toBeInTheDocument();
  await screen.findByText("5월 1주차");
  fireEvent.click(screen.getByRole("button", { name: "재시도" }));
  expect(reload).toHaveBeenCalled();
});

test("renders empty state when items list is empty", async () => {
  renderWith(
    <DiscoverPage
      state={{ status: "ready", data: makeResponse([]) }}
      reload={() => {}}
      today="2026-05-07"
    />,
  );
  expect(screen.getByText("표시할 이슈가 없습니다.")).toBeInTheDocument();
  await screen.findByText("5월 1주차");
});
