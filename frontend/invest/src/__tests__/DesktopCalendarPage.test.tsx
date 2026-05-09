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

function event(overrides: Partial<CalendarEvent> & Pick<CalendarEvent, "eventId" | "title" | "market" | "eventType">): CalendarEvent {
  return {
    source: "fixture",
    relatedSymbols: [],
    relation: "none",
    badges: [],
    ...overrides,
  };
}

const calendarFixture: CalendarResponse = {
  tab: "all",
  fromDate: "2026-05-11",
  toDate: "2026-05-17",
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
    { date: "2026-05-12", events: [], clusters: [] },
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
    { date: "2026-05-14", events: [], clusters: [] },
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
          topEvents: [
            event({ eventId: "evt-cpi", title: "US CPI", market: "us", eventType: "economic" }),
          ],
        },
      ],
    },
    { date: "2026-05-16", events: [], clusters: [] },
    { date: "2026-05-17", events: [], clusters: [] },
  ],
  meta: { warnings: [] },
};

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date("2026-05-11T12:00:00+09:00"));
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    homeSummary: { includedSources: [], excludedSources: [], totalValueKrw: 0 },
    accounts: [], groupedHoldings: [], watchSymbols: [], sourceVisuals: [],
    meta: { warnings: [], watchlistAvailable: true },
  });
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr", asOf: new Date().toISOString(), items: [], meta: { warnings: [] },
  });
  vi.spyOn(calApi, "fetchCalendar").mockResolvedValue(calendarFixture);
  vi.spyOn(calApi, "fetchWeeklySummary").mockResolvedValue({
    weekStart: "2026-05-11", asOf: new Date().toISOString(),
    sections: [], partial: false, missingDates: [],
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

test("fetches target week and renders week rail and weekly summary toggle", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));

  await waitFor(() => {
    expect(calApi.fetchCalendar).toHaveBeenCalledWith({ fromDate: "2026-05-11", toDate: "2026-05-17", tab: "all" });
  });
  await waitFor(() => expect(screen.getAllByTestId(/^day-\d{4}-/)).toHaveLength(7));

  await user.click(screen.getByTestId("open-weekly-summary"));
  await waitFor(() => expect(screen.getByTestId("weekly-summary")).toBeInTheDocument());
});

test("week strip sums cluster eventCount", async () => {
  render(wrap(<DesktopCalendarPage />));

  await waitFor(() => expect(screen.getByTestId("day-2026-05-13")).toBeInTheDocument());
  expect(within(screen.getByTestId("day-2026-05-13")).getByText("327")).toBeInTheDocument();
});

test("renders cluster-only days in the week grouped list", async () => {
  render(wrap(<DesktopCalendarPage />));

  const clusterDay = await screen.findByTestId("calendar-day-section-2026-05-13");
  expect(clusterDay).toHaveAttribute("data-selected", "false");
  expect(within(clusterDay).getByTestId("calendar-cluster")).toBeInTheDocument();
  expect(within(clusterDay).getByText("미국 실적 발표 327건")).toBeInTheDocument();
  expect(within(clusterDay).getByText(/AAPL earnings/)).toBeInTheDocument();
  expect(within(clusterDay).getByText(/MSFT earnings/)).toBeInTheDocument();
  expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
});

test("renders direct events and clusters from different dates together", async () => {
  render(wrap(<DesktopCalendarPage />));

  expect(await screen.findByTestId("calendar-day-section-2026-05-11")).toBeInTheDocument();
  expect(await screen.findByTestId("calendar-day-section-2026-05-13")).toBeInTheDocument();
  expect(screen.getByText("AAPL earnings direct")).toBeInTheDocument();
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
});

test("type and region filters apply to clusters", async () => {
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  render(wrap(<DesktopCalendarPage />));

  expect(await screen.findByText("미국 실적 발표 327건")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "경제지표" }));
  expect(screen.queryByText("미국 실적 발표 327건")).not.toBeInTheDocument();
  expect(screen.getByText("글로벌 경제지표 4건")).toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "실적" }));
  expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
  expect(screen.queryByText("글로벌 경제지표 4건")).not.toBeInTheDocument();

  await user.click(screen.getByRole("button", { name: "국내" }));
  expect(screen.getByTestId("calendar-empty")).toHaveTextContent("선택한 필터에 해당하는 이번 주 일정이 없습니다.");
});
