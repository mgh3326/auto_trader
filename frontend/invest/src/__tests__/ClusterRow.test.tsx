import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ClusterRow } from "../components/calendar/ClusterRow";
import type { CalendarClusterVM, CalendarEventVM } from "../components/calendar/vm";

function topEvent(title: string, id: string): CalendarEventVM {
  return {
    id, date: "2026-05-13", dayOfMonth: 13, monthDay: "5/13",
    type: "earnings", region: "us", title,
    time: null, released: false, actual: null, forecast: null, previous: null,
    own: null, badges: [],
  };
}

function cluster(overrides: Partial<CalendarClusterVM> = {}): CalendarClusterVM {
  return {
    id: "c1",
    date: "2026-05-13",
    dayOfMonth: 13,
    monthDay: "5/13",
    type: "earnings",
    region: "us",
    title: "미국 실적 발표 327건",
    count: 327,
    topEvents: [topEvent("AAPL", "e1"), topEvent("MSFT", "e2"), topEvent("GOOGL", "e3")],
    ...overrides,
  };
}

describe("ClusterRow", () => {
  test("uses the calendar-cluster-row class for media-query rules", () => {
    render(<ClusterRow cluster={cluster()} />);
    expect(screen.getByTestId("calendar-cluster")).toHaveClass("calendar-cluster-row");
  });

  test("title and preview line both opt into __title / __preview classes", () => {
    render(<ClusterRow cluster={cluster()} />);
    expect(screen.getByText("미국 실적 발표 327건")).toHaveClass("calendar-cluster-row__title");
    // Preview line is the line with the dot-joined top events.
    expect(screen.getByText(/AAPL · MSFT · GOOGL 외/)).toHaveClass("calendar-cluster-row__preview");
  });

  test("count chip is removed — cluster title already carries the count", () => {
    render(<ClusterRow cluster={cluster({ count: 327 })} />);
    expect(screen.queryByTestId("calendar-cluster-count")).not.toBeInTheDocument();
    // The cluster title still surfaces the number to the user.
    expect(screen.getByText("미국 실적 발표 327건")).toBeInTheDocument();
    // And no leftover raw +N anywhere.
    expect(screen.getByTestId("calendar-cluster").textContent ?? "").not.toMatch(/\+\d/);
  });

  test("does not nest interactive elements", () => {
    render(<ClusterRow cluster={cluster()} />);
    const row = screen.getByTestId("calendar-cluster");
    expect(row.tagName).toBe("ARTICLE");
    expect(row.querySelectorAll("button, a").length).toBe(0);
  });

  test("falls back to '상세 일정 묶음' if topEvents is empty", () => {
    render(<ClusterRow cluster={cluster({ topEvents: [] })} />);
    expect(screen.getByText("상세 일정 묶음")).toBeInTheDocument();
  });
});
