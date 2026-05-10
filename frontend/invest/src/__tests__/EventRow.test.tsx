import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { EventRow } from "../components/calendar/EventRow";
import type { CalendarEventVM } from "../components/calendar/vm";

function ev(overrides: Partial<CalendarEventVM> = {}): CalendarEventVM {
  return {
    id: "evt-1",
    date: "2026-05-13",
    dayOfMonth: 13,
    monthDay: "5/13",
    type: "earnings",
    region: "us",
    title: "AAPL Q2 earnings",
    time: "오후 9시 발표 예정",
    released: false,
    actual: null,
    forecast: null,
    previous: null,
    own: null,
    badges: [],
    ...overrides,
  };
}

describe("EventRow", () => {
  test("uses the calendar-event-row class so calendar.css media queries apply", () => {
    render(<EventRow ev={ev()} />);
    expect(screen.getByTestId("calendar-event")).toHaveClass("calendar-event-row");
  });

  test("renders no nested interactive elements (no <button>, no <a>)", () => {
    render(<EventRow ev={ev()} />);
    const row = screen.getByTestId("calendar-event");
    expect(row.tagName).toBe("ARTICLE");
    expect(row.querySelectorAll("button, a").length).toBe(0);
  });

  test("very long titles render without forcing horizontal overflow", () => {
    const longTitle = "A".repeat(200);
    render(<EventRow ev={ev({ title: longTitle })} />);
    const titleNode = screen.getByText(longTitle);
    expect(titleNode).toHaveClass("calendar-event-row__title");
  });

  test("renders formatKstTime fallback when time + actuals are all null", () => {
    render(<EventRow ev={ev({ time: null })} />);
    expect(screen.getByText("발표 예정 · KST")).toBeInTheDocument();
  });

  test("uses backend-provided eventTimeLocal verbatim when present", () => {
    render(<EventRow ev={ev({ time: "오전 8시 30분 발표" })} />);
    expect(screen.getByText("오전 8시 30분 발표")).toBeInTheDocument();
  });
});
