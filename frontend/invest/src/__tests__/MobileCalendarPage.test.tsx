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

test("requests the full month grid range (Sun-aligned 6 weeks) on mount, NOT a single week", async () => {
  render(wrap(<MobileCalendarPage />));
  await waitFor(() => {
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({
      fromDate: "2026-04-26",
      toDate: "2026-06-06",
      tab: "all",
    });
  });
});

test("renders CalendarMonthHeader with the current month title and selected-date list", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText("2026년 5월")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-prev-month")).toBeInTheDocument();
  expect(screen.getByTestId("calendar-next-month")).toBeInTheDocument();
  expect(await screen.findByTestId("selected-date-events")).toHaveAttribute(
    "data-selected-date",
    "2026-05-11",
  );
});

test("WeekDateStrip is still rendered for the week containing today", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("week-date-strip")).toBeInTheDocument();
});

test("prev/next month re-fetches the new Sun-aligned grid range", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<MobileCalendarPage />));
  await waitFor(() => expect(calApi.fetchCalendar).toHaveBeenCalledTimes(1));

  await user.click(screen.getByTestId("calendar-next-month"));
  await waitFor(() =>
    expect(calApi.fetchCalendar).toHaveBeenLastCalledWith({
      fromDate: "2026-05-31",
      toDate: "2026-07-11",
      tab: "all",
    }),
  );
});

test("does NOT use UTC fmt — selected date uses fmtLocal even in non-UTC timezone", async () => {
  // 2026-05-11 in KST equals 2026-05-10 in UTC; fmtLocal must give 2026-05-11.
  render(wrap(<MobileCalendarPage />));
  const list = await screen.findByTestId("selected-date-events");
  expect(list).toHaveAttribute("data-selected-date", "2026-05-11");
});

test("filter pills live in a horizontally-scrollable container (not flex-wrap)", async () => {
  render(wrap(<MobileCalendarPage />));
  const filters = await screen.findByTestId("calendar-mobile-filters");
  expect(filters).toHaveClass("calendar-mobile-filters");
  // 3 pills.
  expect(within(filters).getAllByRole("button")).toHaveLength(3);
});

test("error response surfaces the calendar-error banner, not the empty state", async () => {
  vi.spyOn(calApi, "fetchCalendar").mockRejectedValueOnce(new Error("boom"));
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByTestId("calendar-error")).toHaveTextContent("boom");
  expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
});

test("touches an in-month date in the strip and updates the selected-date list", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<MobileCalendarPage />));
  await screen.findByTestId("selected-date-events");
  // 2026-05-13 is in the same week (Mon=11) so it should be in the strip.
  await user.click(screen.getByTestId("day-2026-05-13"));
  await waitFor(() =>
    expect(screen.getByTestId("selected-date-events")).toHaveAttribute(
      "data-selected-date",
      "2026-05-13",
    ),
  );
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
});

test("includes 오늘 prefix on the selected-date label when today is selected", async () => {
  render(wrap(<MobileCalendarPage />));
  expect(await screen.findByText(/오늘 · 5월 11일 월요일 일정/)).toBeInTheDocument();
});
