import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, afterEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { MobileCalendarPage } from "../pages/mobile/MobileCalendarPage";
import * as calApi from "../api/calendar";
import type { CalendarEvent, CalendarResponse } from "../types/calendar";

function event(
  o: Partial<CalendarEvent> & Pick<CalendarEvent, "eventId" | "title" | "market" | "eventType">,
): CalendarEvent {
  return { source: "fixture", relatedSymbols: [], relation: "none", badges: [], ...o };
}

const calendarFixture: CalendarResponse = {
  tab: "all",
  fromDate: "2026-04-26",
  toDate: "2026-06-06",
  asOf: "2026-05-11T03:00:00.000Z",
  days: [
    {
      date: "2026-05-11",
      events: [event({ eventId: "e1", title: "AAPL earnings", market: "us", eventType: "earnings" })],
      clusters: [],
      dataState: "loaded",
    },
    {
      date: "2026-05-13",
      events: [],
      clusters: [
        {
          clusterId: "c1", label: "US earnings", eventType: "earnings", market: "us",
          eventCount: 327,
          topEvents: [event({ eventId: "t1", title: "AAPL", market: "us", eventType: "earnings" })],
        },
      ],
      dataState: "loaded",
    },
  ],
  meta: { warnings: [], sourceFreshness: [], coverage: null },
};

function wrap(ui: React.ReactElement) {
  return (
    <MemoryRouter basename="/invest" initialEntries={["/invest/calendar"]}>
      {ui}
    </MemoryRouter>
  );
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-05-11T12:00:00+09:00"));
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
  render(wrap(<MobileCalendarPage />));
  await waitFor(() => {
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({
      fromDate: "2026-05-08",
      toDate: "2026-05-14",
      tab: "all",
    });
  });
  expect(calApi.fetchCalendar).not.toHaveBeenCalledWith(
    expect.objectContaining({ fromDate: "2026-04-26", toDate: "2026-06-06" }),
  );
});

test("renders CalendarMonthHeader with the current month title and the monthly timeline", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText("2026년 5월")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-prev-month")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-next-month")).toBeInTheDocument();
  await screen.findByTestId("calendar-timeline");
  // 31 day sections for May 2026.
  expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
});

test("WeekDateStrip is still rendered for the week containing today", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("week-date-strip")).toBeInTheDocument();
});

test("ROB-272 Phase 2: prev/next month re-fetches the new selectedDate ±3 window", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<MobileCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));

  // June 2026: today (2026-05-11) is not in the month → selectedDate becomes
  // 2026-06-01 → ±3 = 5/29..6/4.
  await user.click(screen.getByTestId("calendar-next-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      fromDate: "2026-05-29",
      toDate: "2026-06-04",
      tab: "all",
    }),
  );
});

test("does NOT use UTC fmt — today's section uses fmtLocal even in non-UTC timezone", async () => {
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  // The 2026-05-11 section is the data-selected one, not 2026-05-10.
  const may11 = document.querySelector('[data-day-anchor="2026-05-11"]');
  expect(may11).not.toBeNull();
  expect(may11).toHaveAttribute("data-selected", "true");
});

test("filter pills live in a horizontally-scrollable container (not flex-wrap)", async () => {
  render(wrap(<MobileCalendarPage />));
  const filters = await screen.findByTestId("calendar-mobile-filters");
  expect(filters).toHaveClass("calendar-mobile-filters");
  // 3 pills.
  expect(within(filters).getAllByRole("button")).toHaveLength(3);
});

test("error response surfaces the calendar-error banner, not day sections", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("boom"));
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("calendar-error")).toHaveTextContent("boom");
  expect(screen.queryByTestId("calendar-day-section")).not.toBeInTheDocument();
});

test("touches an in-month date in the strip and scrolls/highlights that day section (does not filter)", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  const scrollSpy = vi.fn();
  const originalScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = scrollSpy;
  try {
    render(wrap(<MobileCalendarPage />));
    await screen.findByTestId("calendar-timeline");

    await user.click(screen.getByTestId("day-2026-05-13"));

    // All 31 day sections still in the DOM (no filter-collapse).
    expect(screen.getAllByTestId("calendar-day-section")).toHaveLength(31);
    const may13 = document.querySelector('[data-day-anchor="2026-05-13"]')!;
    expect(may13).toHaveAttribute("data-selected", "true");
    expect(scrollSpy).toHaveBeenCalled();
    // The cluster still shows in the May 13 section, with the top event rendered
    // separately and the overflow count reflecting the remaining events.
    const overflow = within(may13 as HTMLElement).getByTestId("calendar-cluster-overflow");
    expect(overflow).toHaveTextContent("미국 실적 발표");
    expect(overflow).toHaveTextContent("326");
  } finally {
    Element.prototype.scrollIntoView = originalScroll;
  }
});

test("today's day section is labelled with the 오늘 prefix", async () => {
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByText(/오늘 · 5월 11일 \(월\)/)).toBeInTheDocument();
});

test("default surface renders the source button and never the legacy freshness banner", async () => {
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("calendar-timeline");
  expect(screen.getByTestId("calendar-source-button")).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
});
