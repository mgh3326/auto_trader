import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopFeedNewsPage } from "../pages/desktop/DesktopFeedNewsPage";
import * as feedApi from "../api/feedNews";
import * as panelApi from "../api/accountPanel";

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
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
        relatedSymbols: [],
        relation: "none",
        url: "x",
        publisher: "Reuters",
        issueId: "iss-xyz",
      },
    ],
    meta: { warnings: [] },
  });
});

test("renders news items and reacts to tab change", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/feed/news"]}>
      <DesktopFeedNewsPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId("feed-item")).toHaveLength(1));
  await userEvent.click(screen.getByTestId("tab-latest"));
  await waitFor(() => expect(feedApi.fetchFeedNews).toHaveBeenCalledTimes(2));
});

test("renders an issue chip linked to the issue detail page when issueId is present", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/feed/news"]}>
      <DesktopFeedNewsPage />
    </MemoryRouter>,
  );
  const chip = await screen.findByTestId("feed-item-issue-chip");
  expect(chip).toHaveTextContent("삼성전자 실적 발표");
  expect(chip).toHaveAttribute("href", "/invest/app/discover/issues/iss-xyz");
  expect(chip).toHaveAttribute("data-issue-id", "iss-xyz");
});
