// frontend/invest/src/__tests__/DiscoverPage.test.tsx
import type { ReactNode } from "react";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test, vi } from "vitest";
import { DiscoverPage } from "../pages/DiscoverPage";
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

function makeResponse(items: NewsRadarItem[], over: Partial<NewsRadarResponse> = {}): NewsRadarResponse {
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
    ...over,
  };
}

function renderWith(node: ReactNode) {
  return render(<MemoryRouter basename="/invest/app" initialEntries={["/invest/app/discover"]} >{node}</MemoryRouter>);
}

test("renders sorted issue cards with related-news counts", () => {
  const items = [
    makeItem({ id: "a", title: "낮은 이슈", severity: "low",
               risk_category: "macro_policy", briefing_score: 1 }),
    makeItem({ id: "b", title: "높은 이슈", severity: "high",
               risk_category: "macro_policy", briefing_score: 2,
               published_at: "2026-05-07T11:55:00Z" }),
    makeItem({ id: "c", title: "지정학", severity: "high",
               risk_category: "geopolitical_oil", briefing_score: 5,
               published_at: "2026-05-07T11:00:00Z" }),
  ];
  renderWith(
    <DiscoverPage state={{ status: "ready", data: makeResponse(items) }} reload={() => {}} />,
  );

  const titles = screen.getAllByRole("link").map((a) => a.textContent ?? "");
  expect(titles[0]).toContain("지정학");
  expect(titles[1]).toContain("높은 이슈");
  expect(titles[2]).toContain("낮은 이슈");
  // macro_policy bucket has 2 items, geopolitical_oil has 1.
  expect(screen.getAllByText(/관련 뉴스 2개/).length).toBeGreaterThanOrEqual(2);
  expect(screen.getByText(/관련 뉴스 1개/)).toBeInTheDocument();
});

test("renders loading state", () => {
  renderWith(<DiscoverPage state={{ status: "loading" }} reload={() => {}} />);
  expect(screen.getByText("불러오는 중…")).toBeInTheDocument();
});

test("renders error state with retry", () => {
  const reload = vi.fn();
  renderWith(<DiscoverPage state={{ status: "error", message: "boom" }} reload={reload} />);
  expect(screen.getByText("잠시 후 다시 시도해 주세요.")).toBeInTheDocument();
  expect(screen.getByText(/boom/)).toBeInTheDocument();
  screen.getByRole("button", { name: "재시도" }).click();
  expect(reload).toHaveBeenCalled();
});

test("renders empty state when items list is empty", () => {
  renderWith(
    <DiscoverPage
      state={{ status: "ready", data: makeResponse([]) }}
      reload={() => {}}
    />,
  );
  expect(screen.getByText("표시할 이슈가 없습니다.")).toBeInTheDocument();
});

test("renders stale readiness banner", () => {
  renderWith(
    <DiscoverPage
      state={{
        status: "ready",
        data: makeResponse([], {
          readiness: {
            status: "stale",
            latest_scraped_at: null,
            latest_published_at: null,
            recent_6h_count: 0,
            recent_24h_count: 0,
            source_count: 0,
            stale: true,
            max_age_minutes: 120,
            warnings: [],
          },
        }),
      }}
      reload={() => {}}
    />,
  );
  expect(
    screen.getByText("데이터가 최신이 아닐 수 있습니다."),
  ).toBeInTheDocument();
});
