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
    tab: "top", asOf: new Date().toISOString(), issues: [], items: [
      { id: 1, title: "n1", market: "kr", relatedSymbols: [], relation: "none", url: "x", publisher: "Reuters" },
    ], meta: { warnings: [] },
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
