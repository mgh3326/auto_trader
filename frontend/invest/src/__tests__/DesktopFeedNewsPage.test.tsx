import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopFeedNewsPage } from "../pages/desktop/DesktopFeedNewsPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as feedApi from "../api/feedNews";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/feed/news"]}>
        {ui}
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr", asOf: new Date().toISOString(), items: [], meta: { warnings: [] },
  });
  vi.spyOn(feedApi, "fetchFeedNews").mockResolvedValue({
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
          },
        ],
        relation: "watchlist",
        url: "x",
        publisher: "Reuters",
        issueId: "iss-xyz",
      },
    ],
    meta: { warnings: [] },
  });
});

test("renders news items and reacts to tab change", async () => {
  render(wrap(<DesktopFeedNewsPage />));
  await waitFor(() => expect(screen.getAllByTestId("feed-item")).toHaveLength(1));
  await userEvent.click(screen.getByTestId("tab-latest"));
  await waitFor(() => expect(feedApi.fetchFeedNews).toHaveBeenCalledTimes(2));
});

test("renders an issue chip linked to the issue detail page when issueId is present", async () => {
  render(wrap(<DesktopFeedNewsPage />));
  const chip = await screen.findByTestId("feed-item-issue-chip");
  expect(chip).toHaveTextContent("삼성전자 실적 발표");
  // Chip points at the canonical /discover route (Stage 4.2). Legacy
  // /app/discover/issues/:id remains routable for backwards compat.
  expect(chip).toHaveAttribute("href", "/invest/discover/issues/iss-xyz");
  expect(chip).toHaveAttribute("data-issue-id", "iss-xyz");
});


test("renders related symbol chips as read-only badges", async () => {
  render(wrap(<DesktopFeedNewsPage />));
  const chip = await screen.findByTestId("feed-item-related-symbol-chip");
  expect(chip).toHaveTextContent("005930");
  expect(chip).toHaveTextContent("삼성전자");
  expect(chip).toHaveAttribute("data-symbol", "005930");
  expect(chip).toHaveAttribute("data-market", "kr");
  expect(chip).toHaveAttribute("data-relation", "watchlist");
  expect(chip.tagName.toLowerCase()).toBe("span");
});
