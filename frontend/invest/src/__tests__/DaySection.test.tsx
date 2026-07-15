import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { DaySection } from "../components/calendar/DaySection";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

function evt(id: string, title: string): CalendarEventVM {
  return {
    id, date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

function cluster(id: string, count: number): CalendarClusterVM {
  return {
    id, date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
    type: "earnings", region: "us", title: `미국 실적 발표 ${count}건`,
    count, topEvents: [],
  };
}

describe("DaySection (ROB-185)", () => {
  test("renders header with day label and total count when non-empty", () => {
    render(
      <DaySection
        dateIso="2026-05-11"
        todayIso="2026-05-11"
        events={[evt("e1", "AAPL")]}
        clusters={[cluster("c1", 5)]}
        selected={false}
      />,
    );
    const sec = screen.getByTestId("calendar-day-section");
    expect(sec).toHaveAttribute("data-day-anchor", "2026-05-11");
    expect(sec).toHaveTextContent("오늘 · 5월 11일 (월)");
    expect(sec).toHaveTextContent("일정 6개");
  });

  test("renders empty placeholder copy when no events/clusters and hides total", () => {
    render(
      <DaySection
        dateIso="2026-05-12"
        todayIso="2026-05-11"
        events={[]}
        clusters={[]}
        selected={false}
      />,
    );
    const sec = screen.getByTestId("calendar-day-section");
    expect(sec).toHaveTextContent("내일 · 5월 12일 (화)");
    expect(sec).toHaveTextContent("이 날은 예정된 일정이 없어요");
    expect(sec).not.toHaveTextContent("일정 0개");
  });

  test("renders events and clusters (clusters first, events after)", () => {
    render(
      <DaySection
        dateIso="2026-05-13"
        todayIso="2026-05-11"
        events={[evt("e1", "Event A"), evt("e2", "Event B")]}
        clusters={[cluster("c1", 12)]}
        selected={false}
      />,
    );
    expect(within(screen.getByTestId("calendar-day-section")).getAllByTestId("calendar-event")).toHaveLength(2);
    // ROB-186 replaced the aggregate ClusterRow with ClusterEventRows: a cluster
    // whose count exceeds its topEvents renders the overflow indicator row.
    expect(screen.getByTestId("calendar-cluster-overflow")).toBeInTheDocument();
  });

  test("applies data-selected='true' when selected, default 'false' otherwise", () => {
    const { rerender } = render(
      <DaySection
        dateIso="2026-05-15"
        todayIso="2026-05-11"
        events={[]}
        clusters={[]}
        selected
      />,
    );
    expect(screen.getByTestId("calendar-day-section")).toHaveAttribute("data-selected", "true");

    rerender(
      <DaySection
        dateIso="2026-05-15"
        todayIso="2026-05-11"
        events={[]}
        clusters={[]}
        selected={false}
      />,
    );
    expect(screen.getByTestId("calendar-day-section")).toHaveAttribute("data-selected", "false");
  });

  test("header uses sticky CSS class so longer scrolls keep the day label visible", () => {
    render(
      <DaySection
        dateIso="2026-05-11"
        todayIso="2026-05-11"
        events={[evt("e1", "x")]}
        clusters={[]}
        selected={false}
      />,
    );
    const header = screen.getByTestId("calendar-day-section-header");
    expect(header).toHaveClass("calendar-day-section__header");
  });

  test("renders as a semantic <section> with an accessible label", () => {
    render(
      <DaySection
        dateIso="2026-05-11"
        todayIso="2026-05-11"
        events={[evt("e1", "x")]}
        clusters={[]}
        selected={false}
      />,
    );
    const sec = screen.getByTestId("calendar-day-section");
    expect(sec.tagName).toBe("SECTION");
    expect(sec.getAttribute("aria-label")).toContain("5월 11일");
  });
});
