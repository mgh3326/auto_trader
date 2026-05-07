import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopCalendarPage } from "../pages/desktop/DesktopCalendarPage";
import * as calApi from "../api/calendar";
import * as panelApi from "../api/accountPanel";

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(calApi, "fetchCalendar").mockResolvedValue({
    tab: "all", fromDate: "2026-05-04", toDate: "2026-05-10",
    asOf: new Date().toISOString(),
    days: Array.from({ length: 7 }).map((_, i) => ({
      date: `2026-05-${String(4 + i).padStart(2, "0")}`,
      events: i === 0 ? [{ eventId: "e1", title: "AAPL earnings", market: "us", eventType: "earnings", source: "finnhub", relatedSymbols: [], relation: "none", badges: [] }] : [],
      clusters: [],
    })),
    meta: { warnings: [] },
  });
  vi.spyOn(calApi, "fetchWeeklySummary").mockResolvedValue({
    weekStart: "2026-05-04", asOf: new Date().toISOString(),
    sections: [], partial: false, missingDates: [],
  });
});

test("renders week rail and weekly summary toggle", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/calendar"]}>
      <DesktopCalendarPage />
    </MemoryRouter>,
  );
  await waitFor(() => expect(screen.getAllByTestId(/^day-\d{4}-/)).toHaveLength(7));
  await userEvent.click(screen.getByTestId("open-weekly-summary"));
  await waitFor(() => expect(screen.getByTestId("weekly-summary")).toBeInTheDocument());
});
