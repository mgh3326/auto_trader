import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopFeedNewsPage } from "../pages/desktop/DesktopFeedNewsPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as feedApi from "../api/feedNews";
import * as panelApi from "../api/accountPanel";
import type { FeedNewsResponse } from "../types/feedNews";

function feedResponse(overrides: Partial<FeedNewsResponse> = {}): FeedNewsResponse {
  return {
    tab: "top",
    asOf: new Date().toISOString(),
    issues: [
      {
        id: "iss-xyz",
        market: "kr",
        rank: 1,
        issue_title: "삼성전자 실적 발표",
        subtitle: null,
        direction: "up",
        source_count: 3,
        article_count: 2,
        updated_at: new Date().toISOString(),
        summary: null,
        related_symbols: [],
        related_sectors: [],
        articles: [
          {
            id: 1,
            title: "n1",
            url: "x",
            source: "Reuters",
            feed_source: null,
            published_at: null,
            summary: null,
            matched_terms: [],
          },
        ],
        signals: { recency_score: 1, source_diversity_score: 1, mention_score: 1 },
      },
    ],
    items: [
      {
        id: 1,
        title: "n1",
        market: "kr",
        relatedSymbols: [
          {
            symbol: "005930",
            market: "kr",
            displayName: "삼성전자",
            relation: "watchlist",
            matchReason: "alias_dict",
            matchedTerm: "삼성전자",
            quote: { changeRate: 1.23 },
          },
          {
            symbol: "000660",
            market: "kr",
            displayName: "SK하이닉스",
            relation: "none",
            matchReason: "alias_dict",
            matchedTerm: "하닉",
          },
        ],
        relation: "watchlist",
        url: "https://example.com/n1",
        publisher: "Reuters",
        feedSource: "browser_naver_research",
        publishedAt: new Date(Date.now() - 3 * 60 * 60 * 1000).toISOString(),
        issueId: "iss-xyz",
        summarySnippet: "삼성전자 실적 발표 요약입니다.",
      },
    ],
    meta: { warnings: [] },
    ...overrides,
  };
}

function renderPage() {
  return render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/feed/news"]}>
        <DesktopFeedNewsPage />
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [],
    groupedHoldings: [],
    watchSymbols: [],
    sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue(feedResponse());
});

test("renders dense news rows and reacts to tab change", async () => {
  renderPage();

  expect(await screen.findByTestId("right-remote-panel")).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "내 투자" })).toHaveAttribute("aria-selected", "true");
  expect(screen.getByRole("tab", { name: "관심" })).toBeInTheDocument();

  const row = await screen.findByTestId("feed-item");
  expect(row).toHaveTextContent("n1");
  expect(row).toHaveTextContent("Reuters");
  expect(row).toHaveTextContent("KR");
  expect(row).toHaveTextContent("시간 전");

  await userEvent.click(screen.getByTestId("tab-latest"));
  await waitFor(() => expect(feedApi.fetchFeedNews).toHaveBeenCalledTimes(2));
});

test("renders an issue chip linked to the issue detail page when issueId is present", async () => {
  renderPage();

  const chip = await screen.findByTestId("feed-item-issue-chip");
  expect(chip).toHaveTextContent("삼성전자 실적 발표");
  // Chip points at the canonical /discover route (Stage 4.2). Legacy
  // /app/discover/issues/:id remains routable for backwards compat.
  expect(chip).toHaveAttribute("href", "/invest/discover/issues/iss-xyz");
  expect(chip).toHaveAttribute("data-issue-id", "iss-xyz");
  expect(chip.tagName.toLowerCase()).toBe("a");
});

test("renders related symbol chips as read-only badges with optional quote seam", async () => {
  renderPage();

  const chips = await screen.findAllByTestId("feed-item-related-symbol-chip");
  expect(chips).toHaveLength(2);
  const quotedChip = chips[0]!;
  const unquotedChip = chips[1]!;

  expect(quotedChip).toHaveTextContent("005930");
  expect(quotedChip).toHaveTextContent("삼성전자");
  expect(quotedChip).toHaveTextContent("관심");
  expect(quotedChip).toHaveTextContent("+1.23%");
  expect(quotedChip).toHaveAttribute("data-symbol", "005930");
  expect(quotedChip).toHaveAttribute("data-market", "kr");
  expect(quotedChip).toHaveAttribute("data-relation", "watchlist");
  expect(quotedChip.tagName.toLowerCase()).toBe("span");

  expect(unquotedChip).toHaveTextContent("000660");
  expect(unquotedChip).not.toHaveTextContent("%");
  expect(unquotedChip.tagName.toLowerCase()).toBe("span");
});

test("expands summaries from a sibling toggle without nested interactive elements", async () => {
  renderPage();

  const row = await screen.findByTestId("feed-item");
  expect(row.querySelectorAll("button a, a button, button button, a a")).toHaveLength(0);
  expect(row.querySelector("button [data-testid='feed-item-issue-chip']")).toBeNull();

  await userEvent.click(screen.getByRole("button", { name: "n1 요약 더보기" }));
  expect(await screen.findByTestId("feed-item-summary")).toHaveTextContent("삼성전자 실적 발표 요약입니다.");
});

test("renders loading state before feed news resolves", async () => {
  vi.spyOn(feedApi, "fetchFeedNews").mockReturnValue(new Promise(() => undefined));

  renderPage();

  expect(await screen.findByTestId("feed-news-loading")).toHaveTextContent("최신 뉴스를 불러오는 중입니다");
});

test("renders reason-specific empty state", async () => {
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue(
    feedResponse({ items: [], issues: [], meta: { warnings: [], emptyReason: "no_matching_news" } }),
  );

  renderPage();

  expect(await screen.findByTestId("feed-news-empty")).toHaveTextContent("조건에 맞는 뉴스가 없습니다.");
});

// ROB-172: symbol chip uses relatedSymbols[].market as asset market authority
test("ROB-172: symbol chip market badge reflects relatedSymbols market, not source feed market", async () => {
  // KR-sourced article with a US-listed NVIDIA symbol
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue(
    feedResponse({
      items: [
        {
          id: 2,
          title: "엔비디아 관련 뉴스",
          market: "kr",
          sourceMarket: "kr",
          relatedSymbols: [
            {
              symbol: "NVDA",
              market: "us",
              displayName: "NVIDIA",
              relation: "none",
            },
          ],
          relation: "none",
          url: "https://example.com/nvda-kr",
        },
      ],
      issues: [],
    }),
  );

  renderPage();

  const chip = await screen.findByTestId("feed-item-related-symbol-chip");
  // Chip uses relatedSymbols[].market = "us", not source market "kr"
  expect(chip).toHaveAttribute("data-market", "us");
  expect(chip).toHaveTextContent("NVDA");
  expect(chip).toHaveTextContent("· US");
  // Source market line should show KR (the feed origin)
  const sourceMarket = await screen.findByTestId("feed-item-source-market");
  expect(sourceMarket).toHaveTextContent("KR");
});

test("ROB-172: source/feed market does not override chip market — NVDA chip stays US even when source is KR", async () => {
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue(
    feedResponse({
      items: [
        {
          id: 3,
          title: "Naver article about NVIDIA",
          market: "kr",
          sourceMarket: "kr",
          relatedSymbols: [
            {
              symbol: "NVDA",
              market: "us",
              displayName: "NVIDIA",
              relation: "none",
            },
          ],
          relation: "none",
          url: "https://example.com/nvda-naver",
        },
      ],
      issues: [],
    }),
  );

  renderPage();

  const chip = await screen.findByTestId("feed-item-related-symbol-chip");
  expect(chip).toHaveAttribute("data-market", "us");
  // Chip must not show "· KR" (which would mean source market leaked into chip)
  const marketBadge = chip.querySelector("[data-testid='feed-item-symbol-market']");
  expect(marketBadge).toHaveTextContent("· US");
  expect(marketBadge).not.toHaveTextContent("KR");
});

test("ROB-172: KR-source NVIDIA fixture renders NVDA · US chip", async () => {
  // Mirrors the production scenario: Naver (KR feed) article mentioning NVIDIA (US asset)
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue(
    feedResponse({
      items: [
        {
          id: 9659,
          title: "엔비디아, 신제품 발표",
          market: "kr",
          sourceMarket: "kr",
          publisher: "네이버 뉴스",
          relatedSymbols: [
            {
              symbol: "NVDA",
              market: "us",
              displayName: "NVIDIA",
              relation: "none",
            },
          ],
          relation: "none",
          url: "https://example.com/nvda-9659",
        },
      ],
      issues: [],
    }),
  );

  renderPage();

  const chip = await screen.findByTestId("feed-item-related-symbol-chip");
  expect(chip).toHaveAttribute("data-symbol", "NVDA");
  expect(chip).toHaveAttribute("data-market", "us");
  expect(chip).toHaveTextContent("NVDA");
  expect(chip).toHaveTextContent("· US");
  // Ensure the article-level source market shows KR (feed origin)
  expect(await screen.findByTestId("feed-item-source-market")).toHaveTextContent("KR");
});
