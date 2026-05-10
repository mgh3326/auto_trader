import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import * as feedApi from "../api/feedNews";
import * as feedResearchApi from "../api/feedResearch";
import { MobileFeedNewsPage } from "../pages/mobile/MobileFeedNewsPage";
import type { FeedNewsResponse } from "../types/feedNews";
import type { FeedResearchResponse } from "../types/feedResearch";

function feedResponse(): FeedNewsResponse {
  return {
    tab: "top",
    asOf: "2026-05-10T00:00:00Z",
    issues: [],
    items: [
      {
        id: 1,
        title: "모바일 뉴스",
        market: "kr",
        sourceMarket: "kr",
        relatedSymbols: [],
        relation: "none",
        url: "https://example.com/news/1",
      },
    ],
    meta: { warnings: [] },
  };
}

function researchResponse(): FeedResearchResponse {
  return {
    tab: "latest",
    asOf: "2026-05-10T00:00:00Z",
    items: [
      {
        id: 1,
        source: "kis_research",
        title: "모바일 리서치",
        analyst: "홍길동",
        publishedAtText: "2026.05.10",
        category: "산업분석",
        detailUrl: "https://example.com/research/mobile",
        pdfUrl: "https://example.com/research/mobile.pdf",
        excerpt: "모바일에서도 compact citation만 표시합니다.",
        symbolCandidates: [{ symbol: "000660", market: "kr", displayName: "SK하이닉스" }],
        attributionPublisher: "Korea Investment & Securities",
        relation: "mine",
      },
    ],
    meta: { limit: 30, appliedFilters: {} },
  };
}

function renderPage() {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/feed/news"]}>
      <MobileFeedNewsPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue(feedResponse());
  vi.spyOn(feedResearchApi, "fetchFeedResearch").mockResolvedValue(researchResponse());
});

test("renders research cards in the mobile feed seam", async () => {
  renderPage();

  expect(await screen.findByText("모바일 뉴스")).toBeInTheDocument();
  await userEvent.click(screen.getByTestId("tab-research"));

  await waitFor(() => expect(feedResearchApi.fetchFeedResearch).toHaveBeenCalledWith(expect.objectContaining({ tab: "latest", limit: 30 })));
  expect(feedApi.fetchFeedNews).toHaveBeenCalledTimes(1);

  const row = await screen.findByTestId("research-feed-item");
  expect(row).toHaveTextContent("모바일 리서치");
  expect(row).toHaveTextContent("리서치");
  expect(row).toHaveTextContent("산업분석");
  expect(row).toHaveTextContent("홍길동");
  expect(row).toHaveTextContent("보유");
  expect(row).toHaveTextContent("모바일에서도 compact citation만 표시합니다.");
  expect(screen.getByRole("link", { name: "모바일 리서치" })).toHaveAttribute(
    "href",
    "https://example.com/research/mobile",
  );
  expect(screen.getByRole("link", { name: "원문 PDF" })).toHaveAttribute(
    "href",
    "https://example.com/research/mobile.pdf",
  );
  const chip = screen.getByTestId("research-symbol-chip");
  expect(chip).toHaveAttribute("data-symbol", "000660");
  expect(chip).toHaveAttribute("data-market", "kr");
  expect(chip).toHaveTextContent("SK하이닉스");
  expect(row.querySelectorAll("button a, a button, button button, a a")).toHaveLength(0);
});
