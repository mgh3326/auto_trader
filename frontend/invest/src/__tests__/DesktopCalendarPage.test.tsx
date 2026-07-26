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
      dataState: "loaded",
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
      dataState: "loaded",
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
      dataState: "loaded",
    },
  ],
  meta: { warnings: [], sourceFreshness: [], coverage: null },
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

test("ROB-272 Phase 2: initial fetch is selectedDate ±3 (7 days), not the 6-week grid", async () => {
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => {
    // Today is 2026-05-11, selectedDate defaults to today → ±3 = 5/8..5/14.
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({
      fromDate: "2026-05-08",
      toDate: "2026-05-14",
      tab: "all",
    });
  });
  // We must NOT have fired the legacy 42-day grid fetch.
  expect(calApi.fetchCalendar).not.toHaveBeenCalledWith(
    expect.objectContaining({ fromDate: "2026-04-26", toDate: "2026-06-06" }),
  );
  // Grid still renders all 42 cells (per Sunday-aligned grid).
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

test("renders the monthly timeline with one section per in-month day", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  // May 2026 has 31 days.
  expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
  // Today's section reflects the AAPL event.
  const today = screen.getByText(/오늘 · 5월 11일 \(월\)/).closest('[data-testid="calendar-day-section"]') as HTMLElement;
  expect(within(today).getByText("AAPL earnings direct")).toBeInTheDocument();
});

test("clicking a grid cell sets selectedDate as scroll target (does not filter the feed away)", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  // Patch scrollIntoView so we can detect the scroll without jsdom failing.
  const scrollSpy = vi.fn();
  const originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scrollSpy;
  try {
    render(wrap(<DesktopCalendarPage />));
    await screen.findByTestId("calendar-timeline");

    await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));

    // All sections still in the DOM (no filter-to-one-day collapse).
    expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
    // The May 13 section is now data-selected="true"; others are "false".
    const may13 = document.querySelector('[data-day-anchor="2026-05-13"]')!;
    const may11 = document.querySelector('[data-day-anchor="2026-05-11"]')!;
    expect(may13).toHaveAttribute("data-selected", "true");
    expect(may11).toHaveAttribute("data-selected", "false");
    // And we scrolled.
    expect(scrollSpy).toHaveBeenCalled();
    // May 13's cluster is still visible in its section.
    expect(within(may13 as HTMLElement).getByText("미국 실적 발표 · 그 외 325건")).toBeInTheDocument();
  } finally {
    Element.prototype.scrollIntoView = originalScroll;
  }
});

test("days with no matching events render the Toss-friendly empty placeholder, not the freshness banner", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  const may12 = document.querySelector('[data-day-anchor="2026-05-12"]')!;
  expect(within(may12 as HTMLElement).getByText("이 날은 예정된 일정이 없어요")).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
});

test("prev/next month navigation refetches the new selectedDate ±3 window (ROB-272 Phase 2)", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));

  // June 2026: today not in month → selectedDate becomes June 1 → ±3 = 5/29..6/4.
  await user.click(screen.getByTestId("calendar-next-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      fromDate: "2026-05-29",
      toDate: "2026-06-04",
      tab: "all",
    }),
  );

  // Back to May → today (2026-05-11) is in month → ±3 = 5/8..5/14.
  await user.click(screen.getByTestId("calendar-prev-month"));
  // April → first day → ±3 = 3/29..4/4.
  await user.click(screen.getByTestId("calendar-prev-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      fromDate: "2026-03-29",
      toDate: "2026-04-04",
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

test("renders the calendar-loading skeleton while the first fetch is in flight, replaces with the timeline", async () => {
  let resolve: (v: typeof calendarFixture) => void;
  vi.spyOn(calApi, "fetchCalendar").mockImplementationOnce(
    () => new Promise((r) => { resolve = r; }),
  );
  render(wrap(<DesktopCalendarPage />));
  expect(await screen.findByTestId("calendar-loading")).toBeInTheDocument();
  resolve!(calendarFixture);
  await waitFor(() =>
    expect(screen.queryByTestId("calendar-loading")).not.toBeInTheDocument(),
  );
  expect(screen.getByTestId("calendar-timeline")).toBeInTheDocument();
});

test("renders calendar-error banner when fetchCalendar rejects", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("network blew up"));
  render(wrap(<DesktopCalendarPage />));
  const banner = await screen.findByTestId("calendar-error");
  expect(banner).toHaveTextContent("network blew up");
  // No timeline sections render when the request fails.
  expect(screen.queryByTestId("calendar-day-section")).not.toBeInTheDocument();
});

test("today's day section is labelled with 오늘 prefix", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByText(/오늘 · 5월 11일 \(월\)/)).toBeInTheDocument();
});

test("default surface renders the source button and never the legacy freshness banner", async () => {
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByTestId("calendar-source-button")).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
});

test("ROB-272 Phase 2: clicking a date inside the initial window does NOT trigger a duplicate fetch", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));
  // 2026-05-13 is in the initial ±3 window (5/8..5/14) and just got loaded.
  await user.click(screen.getByTestId("month-grid-cell-2026-05-13"));
  // No additional fetch — dedupe must hold.
  expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1);
});

test("ROB-272 Phase 2: clicking a date outside the initial window lazy-loads exactly that day", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));
  // 2026-05-22 is outside the initial 5/8..5/14 window. Clicks ensure only
  // the clicked day; surrounding context comes from the viewport observer
  // once the timeline scrolls into place.
  await user.click(screen.getByTestId("month-grid-cell-2026-05-22"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      fromDate: "2026-05-22",
      toDate: "2026-05-22",
      tab: "all",
    }),
  );
});

test("ROB-272 Phase 2: clicking an out-of-month grid cell moves monthCursor to that month and sets selectedDate", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  // Patch scrollIntoView so the auto-scroll on selectedDate change doesn't blow up jsdom.
  const scrollSpy = vi.fn();
  const originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scrollSpy;
  try {
    render(wrap(<DesktopCalendarPage />));
    await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));
    // The Sunday-aligned 6-week May 2026 grid includes 2026-06-03 in its
    // bottom row as an out-of-month cell. Clicking it should jump to June.
    await user.click(screen.getByTestId("month-grid-cell-2026-06-03"));
    // Month header now reads June.
    await screen.findByText("2026년 6월");
    // selectedDate followed the click, and the new month's anchor ±3 fetch
    // fires for 2026-06-03 ± 3 = 2026-05-31..2026-06-06.
    await waitFor(() =>
      expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
        fromDate: "2026-05-31",
        toDate: "2026-06-06",
        tab: "all",
      }),
    );
  } finally {
    Element.prototype.scrollIntoView = originalScroll;
  }
});

test("type and region filters hide non-matching items from each day section, grid count stays accurate", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));
  await screen.findByTestId("calendar-timeline");

  // Baseline: May 13 cluster present somewhere.
  expect(screen.getByText("미국 실적 발표 · 그 외 325건")).toBeInTheDocument();

  // Filter to 경제지표 — earnings cluster gone, day section still rendered with the empty placeholder.
  await user.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.queryByText("미국 실적 발표 · 그 외 325건")).not.toBeInTheDocument();
  const may13 = document.querySelector('[data-day-anchor="2026-05-13"]')!;
  expect(within(may13 as HTMLElement).getByText("이 날은 예정된 일정이 없어요")).toBeInTheDocument();
  // Grid count badge for May 13 is gone.
  const cell = screen.getByTestId("month-grid-cell-2026-05-13");
  expect(within(cell).queryByText("327")).not.toBeInTheDocument();

  // Switch to 실적 — cluster reappears.
  await user.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.getByText("미국 실적 발표 · 그 외 325건")).toBeInTheDocument();

  // 국내 region filter — empty (cluster is US).
  await user.click(screen.getByRole("button", { name: "국내" }));
  expect(screen.queryByText("미국 실적 발표 · 그 외 325건")).not.toBeInTheDocument();
  expect(within(may13 as HTMLElement).getByText("이 날은 예정된 일정이 없어요")).toBeInTheDocument();
});
