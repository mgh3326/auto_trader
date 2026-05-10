import { render, screen, within } from "@testing-library/react";
import { describe, expect, test, vi } from "vitest";
import { MonthlyEventsTimeline } from "../components/calendar/MonthlyEventsTimeline";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

function evt(id: string, dateIso: string, title: string): CalendarEventVM {
  const day = Number.parseInt(dateIso.slice(8, 10), 10);
  const month = Number.parseInt(dateIso.slice(5, 7), 10);
  return {
    id, date: dateIso, dayOfMonth: day, monthDay: `${month}/${day}`,
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

function cluster(id: string, dateIso: string, count: number): CalendarClusterVM {
  const day = Number.parseInt(dateIso.slice(8, 10), 10);
  const month = Number.parseInt(dateIso.slice(5, 7), 10);
  return {
    id, date: dateIso, dayOfMonth: day, monthDay: `${month}/${day}`,
    type: "earnings", region: "us", title: `미국 실적 발표 ${count}건`,
    count, topEvents: [],
  };
}

const baseProps = {
  monthCursor: new Date(2026, 4, 1), // May 2026
  selectedDate: "2026-05-11",
  todayIso: "2026-05-11",
  filteredByDate: new Map<string, { events: CalendarEventVM[]; clusters: CalendarClusterVM[]; total: number }>(),
};

describe("MonthlyEventsTimeline", () => {
  test("renders one section per in-month day (31 for May 2026)", () => {
    render(<MonthlyEventsTimeline {...baseProps} />);
    const sections = screen.getAllByTestId("calendar-day-section");
    expect(sections).toHaveLength(31);
    expect(sections[0]).toHaveAttribute("data-day-anchor", "2026-05-01");
    expect(sections[30]).toHaveAttribute("data-day-anchor", "2026-05-31");
  });

  test("loading prop renders a single skeleton block, not 31 sections", () => {
    render(<MonthlyEventsTimeline {...baseProps} loading />);
    expect(screen.getByTestId("calendar-loading")).toBeInTheDocument();
    expect(screen.queryAllByTestId("calendar-day-section")).toHaveLength(0);
  });

  test("error prop renders the error banner, not sections", () => {
    render(<MonthlyEventsTimeline {...baseProps} error="boom" />);
    expect(screen.getByTestId("calendar-error")).toHaveTextContent("boom");
    expect(screen.queryAllByTestId("calendar-day-section")).toHaveLength(0);
  });

  test("empty filteredByDate renders all 31 day sections, each with the empty placeholder", () => {
    render(<MonthlyEventsTimeline {...baseProps} />);
    const sections = screen.getAllByTestId("calendar-day-section");
    expect(sections).toHaveLength(31);
    expect(sections[0]).toHaveTextContent("이 날은 예정된 일정이 없어요");
    // The month-level "empty" copy is rendered above the sections when total == 0.
    expect(screen.getByTestId("calendar-timeline-empty")).toHaveTextContent(
      "이번 달은 예정된 주요 일정이 없어요",
    );
  });

  test("non-empty filteredByDate hydrates the matching section's events/clusters", () => {
    const filtered = new Map<string, { events: CalendarEventVM[]; clusters: CalendarClusterVM[]; total: number }>([
      ["2026-05-11", { events: [evt("e1", "2026-05-11", "AAPL")], clusters: [], total: 1 }],
      ["2026-05-13", { events: [], clusters: [cluster("c1", "2026-05-13", 4)], total: 4 }],
    ]);
    render(<MonthlyEventsTimeline {...baseProps} filteredByDate={filtered} />);
    expect(screen.queryByTestId("calendar-timeline-empty")).not.toBeInTheDocument();
    const may11 = document.querySelector('[data-day-anchor="2026-05-11"]')!;
    expect(within(may11 as HTMLElement).getByText("AAPL")).toBeInTheDocument();
    // May 13 cluster present somewhere in the timeline.
    expect(screen.getByText("미국 실적 발표 4건")).toBeInTheDocument();
  });

  test("selectedDate flags only the matching section", () => {
    render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-15" />);
    const may15 = document.querySelector('[data-day-anchor="2026-05-15"]');
    const may11 = document.querySelector('[data-day-anchor="2026-05-11"]');
    expect(may15).toHaveAttribute("data-selected", "true");
    expect(may11).toHaveAttribute("data-selected", "false");
  });

  test("changing selectedDate calls scrollIntoView on the matching section", () => {
    const spy = vi.fn();
    // Patch Element.prototype so any scrollIntoView call is captured.
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      const { rerender } = render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-11" />);
      spy.mockClear();
      rerender(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-20" />);
      expect(spy).toHaveBeenCalledTimes(1);
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });

  test("does not scroll on first mount (initial render is not a navigation)", () => {
    const spy = vi.fn();
    const original = Element.prototype.scrollIntoView;
    Element.prototype.scrollIntoView = spy;
    try {
      render(<MonthlyEventsTimeline {...baseProps} selectedDate="2026-05-15" />);
      expect(spy).not.toHaveBeenCalled();
    } finally {
      Element.prototype.scrollIntoView = original;
    }
  });
});
