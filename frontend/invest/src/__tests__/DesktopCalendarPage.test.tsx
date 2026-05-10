import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, afterEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { DesktopCalendarPage } from "../pages/desktop/DesktopCalendarPage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as calApi from "../api/calendar";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";
import type { CalendarEvent, CalendarResponse } from "../types/calendar";

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/calendar"]}>
        {ui}
      </MemoryRouter>
    </AccountPanelProvider>
  );
}

function event(
  overrides: Partial<CalendarEvent> & Pick<CalendarEvent, "eventId" | "title" | "market" | "eventType">,
): CalendarEvent {
  return {
    source: "fixture",
    relatedSymbols: [],
    relation: "none",
    badges: [],
    ...overrides,
  };
}

// Returned for any month-range fetch in this test file. We reuse the same days fixture to keep tests focused.
const calendarFixture: CalendarResponse = {
  tab: "all",
  fromDate: "2026-04-26",
  toDate: "2026-06-06",
  asOf: "2026-05-11T03:00:00.000Z",
  days: [
    {
      date: "2026-05-11",
      events: [
        event({
          eventId: "evt-aapl-direct",
          title: "AAPL earnings direct",
          market: "us",
          eventType: "earnings",
          eventTimeLocal: "오후 9시 발표 예정",
        }),
      ],
      clusters: [],
    },
    {
      date: "2026-05-13",
      events: [],
      clusters: [
        {
          clusterId: "cluster-us-earnings-2026-05-13",
          label: "US earnings",
          eventType: "earnings",
          market: "us",
          eventCount: 327,
          topEvents: [
            event({ eventId: "evt-aapl-top", title: "AAPL earnings", market: "us", eventType: "earnings" }),
            event({ eventId: "evt-msft-top", title: "MSFT earnings", market: "us", eventType: "earnings" }),
          ],
        },
      ],
    },
    {
      date: "2026-05-15",
      events: [],
      clusters: [
        {
          clusterId: "cluster-global-macro-2026-05-15",
          label: "Global macro",
          eventType: "economic",
          market: "global",
          eventCount: 4,
          topEvents: [event({ eventId: "evt-cpi", title: "US CPI", market: "us", eventType: "economic" })],
        },
      ],
    },
  ],
  meta: { warnings: [] },
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-05-11T12:00:00+09:00"));
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [],
    groupedHoldings: [],
    watchSymbols: [],
    sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr",
    asOf: new Date().toISOString(),
    items: [],
    meta: { warnings: [] },
  });
  vi.spyOn(calApi, "fetchCalendar").mockResolvedValue(calendarFixture);
  vi.spyOn(calApi, "fetchWeeklySummary").mockResolvedValue({
    weekStart: "2026-05-11",
    asOf: new Date().toISOString(),
    sections: [],
    partial: false,
    missingDates: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("fetches the full month grid range (Sun-aligned 6 weeks) on mount", async () => {
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => {
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({
      fromDate: "2026-04-26",
      toDate: "2026-06-06",
      tab: "all",
    });
  });
  expect(screen.getAllByTestId(/^month-grid-cell-/)).toHaveLength(42);
});

test("today highlight and default selected date is today (in-range)", async () => {
  render(wrap(<DesktopCalendarPage />));
  const today = await screen.findByTestId("month-grid-cell-2026-05-11");
  expect(today).toHaveAttribute("data-today", "true");
  expect(today).toHaveAttribute("data-selected", "true");
});

test("month grid shows count derived from clusters and events", async () => {
  render(wrap(<DesktopCalendarPage />));
  const clusterCell = await screen.findByTestId("month-grid-cell-2026-05-13");
  expect(within(clusterCell).getByText("327")).toBeInTheDocument();
});

test("clicking a date updates the selected-date list", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("selected-date-events");

  await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));

  await waitFor(() =>
    expect(screen.getByTestId("selected-date-events")).toHaveAttribute("data-selected-date", "2026-05-13"),
  );
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
  expect(screen.getByText(/5월 13일 수요일 일정/)).toBeInTheDocument();
});

test("empty selected date renders graceful empty state", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("selected-date-events");

  await user.click(screen.getByTestId("month-grid-cell-2026-05-12"));

  expect(await screen.findByTestId("calendar-empty")).toHaveTextContent(
    "선택한 날짜에 일정이 없습니다.",
  );
});

test("prev/next month navigation refetches with the new month range", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));

  await user.click(screen.getByTestId("calendar-next-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      // June 2026 grid: starts Sun 2026-05-31, ends Sat 2026-07-11
      fromDate: "2026-05-31",
      toDate: "2026-07-11",
      tab: "all",
    }),
  );

  await user.click(screen.getByTestId("calendar-prev-month"));
  await user.click(screen.getByTestId("calendar-prev-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      // April 2026 grid: starts Sun 2026-03-29, ends Sat 2026-05-09
      fromDate: "2026-03-29",
      toDate: "2026-05-09",
      tab: "all",
    }),
  );
});

test("month title label shows '2026년 5월' and section header '5월 금융 캘린더'", async () => {
  render(wrap(<DesktopCalendarPage />));
  expect(await screen.findByText("2026년 5월")).toBeInTheDocument();
  expect(screen.getByText("5월 금융 캘린더")).toBeInTheDocument();
});

test("AI weekly card refetches when selecting a date in a different week", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  // Initial selected date = today 2026-05-11 (Mon) -> week_start = 2026-05-11
  await user.click(screen.getByTestId("open-weekly-summary"));
  await waitFor(() =>
    expect(calApi.fetchWeeklySummary).toHaveBeenLastCalledWith("2026-05-11"),
  );

  // Select 2026-05-20 (Wed of next week) -> week_start should be 2026-05-18
  await user.click(screen.getByTestId("month-grid-cell-2026-05-20"));
  await waitFor(() =>
    expect(calApi.fetchWeeklySummary).toHaveBeenLastCalledWith("2026-05-18"),
  );
});

test("type and region filters apply to the selected-date list and grid count", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("selected-date-events");

  // Select May 13 — has 327 US-earnings cluster. Filter to 경제지표 — list/cluster disappears.
  await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  expect(screen.getByTestId("calendar-empty")).toBeInTheDocument();
  // Grid count badge for May 13 should be gone now (no macro events that day).
  const may13 = screen.getByTestId("month-grid-cell-2026-05-13");
  expect(within(may13).queryByText("327")).not.toBeInTheDocument();

  // Switch to 실적 — cluster reappears for May 13.
  await user.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();

  // 국내 region filter — empty (cluster is US).
  await user.click(screen.getByRole("button", { name: "국내" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  expect(screen.getByTestId("calendar-empty")).toBeInTheDocument();
});
