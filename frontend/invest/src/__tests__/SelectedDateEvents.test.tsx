import { render, screen, within } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { SelectedDateEvents } from "../components/calendar/SelectedDateEvents";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

const baseProps = {
  dateLabel: "5월 11일 월요일 일정",
  dateIso: "2026-05-11",
  events: [] as CalendarEventVM[],
  clusters: [] as CalendarClusterVM[],
  emptyMessage: "선택한 날짜에 일정이 없습니다.",
};

function evt(id: string, title: string): CalendarEventVM {
  return {
    id, date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [], displayPriority: 0, highlightReasons: [],
  };
}

describe("SelectedDateEvents", () => {
  test("loading state renders skeleton, not empty/error/populated", () => {
    render(<SelectedDateEvents {...baseProps} loading />);
    expect(screen.getByTestId("calendar-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
    expect(screen.queryByTestId("calendar-error")).not.toBeInTheDocument();
    expect(screen.queryByTestId("calendar-event")).not.toBeInTheDocument();
  });

  test("error state renders the error banner with the message", () => {
    render(<SelectedDateEvents {...baseProps} error="서버에 연결할 수 없습니다" />);
    const banner = screen.getByTestId("calendar-error");
    expect(banner).toHaveTextContent("서버에 연결할 수 없습니다");
    expect(screen.queryByTestId("calendar-loading")).not.toBeInTheDocument();
    expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
  });

  test("empty state renders EmptyEventState with the configured message", () => {
    render(<SelectedDateEvents {...baseProps} />);
    expect(screen.getByTestId("calendar-empty")).toHaveTextContent(
      "선택한 날짜에 일정이 없습니다.",
    );
  });

  test("populated state renders events and exposes day-events test id", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        events={[evt("e1", "AAPL earnings"), evt("e2", "MSFT earnings")]}
      />,
    );
    const list = screen.getByTestId("day-events");
    expect(within(list).getAllByTestId("calendar-event")).toHaveLength(2);
    expect(screen.queryByTestId("calendar-empty")).not.toBeInTheDocument();
  });

  test("header includes dateIso, dateLabel, and total count even with clusters", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        clusters={[
          {
            id: "c1", date: "2026-05-11", dayOfMonth: 11, monthDay: "5/11",
            type: "earnings", region: "us", title: "미국 실적 발표 327건",
            count: 327, topEvents: [],
          },
        ]}
      />,
    );
    const root = screen.getByTestId("selected-date-events");
    expect(root).toHaveAttribute("data-selected-date", "2026-05-11");
    expect(root).toHaveTextContent("5월 11일 월요일 일정");
    expect(root).toHaveTextContent("327건");
  });

  test("renders neutral day summary and overflow label when provided", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        events={[evt("e1", "AAPL earnings"), evt("e2", "MSFT earnings")]}
        summary={{
          headline: "주요 일정 2개 · 그 외 7개",
          highlightEventIds: ["e1", "e2"],
          overflowCount: 7,
          overflowLabel: "그 외 7개",
        }}
      />,
    );
    expect(screen.getByTestId("calendar-day-summary")).toHaveTextContent("주요 일정 2개 · 그 외 7개");
    expect(screen.getByText(/2026-05-11 · 2건 · 그 외 7개/)).toBeInTheDocument();
  });

  test("loading + populated together still shows skeleton (loading wins)", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        loading
        events={[evt("e1", "stale")]}
      />,
    );
    expect(screen.getByTestId("calendar-loading")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });

  test("error wins over populated (so users see the failure, not stale data)", () => {
    render(
      <SelectedDateEvents
        {...baseProps}
        error="boom"
        events={[evt("e1", "stale")]}
      />,
    );
    expect(screen.getByTestId("calendar-error")).toBeInTheDocument();
    expect(screen.queryByText("stale")).not.toBeInTheDocument();
  });
});
