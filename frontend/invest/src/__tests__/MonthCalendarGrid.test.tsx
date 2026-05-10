import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { MonthCalendarGrid } from "../components/calendar/MonthCalendarGrid";

const baseProps = {
  monthCursor: new Date(2026, 4, 1), // May 2026
  selectedDate: "2026-05-13",
  today: "2026-05-11",
  countByDate: new Map<string, number>([
    ["2026-05-11", 3],
    ["2026-05-13", 327],
  ]),
};

describe("MonthCalendarGrid", () => {
  test("renders 42 day cells aligned Sunday-first starting 2026-04-26", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const cells = screen.getAllByTestId(/^month-grid-cell-/);
    expect(cells).toHaveLength(42);
    expect(cells[0]).toHaveAttribute("data-date", "2026-04-26");
    expect(cells[41]).toHaveAttribute("data-date", "2026-06-06");
  });

  test("flags out-of-month, today, and selected cells", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    expect(screen.getByTestId("month-grid-cell-2026-04-26")).toHaveAttribute("data-out-of-month", "true");
    expect(screen.getByTestId("month-grid-cell-2026-05-01")).toHaveAttribute("data-out-of-month", "false");
    expect(screen.getByTestId("month-grid-cell-2026-05-11")).toHaveAttribute("data-today", "true");
    expect(screen.getByTestId("month-grid-cell-2026-05-13")).toHaveAttribute("data-selected", "true");
  });

  test("renders count badge from countByDate", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const cell = screen.getByTestId("month-grid-cell-2026-05-13");
    expect(cell).toHaveTextContent("13");
    expect(cell).toHaveTextContent("327");
  });

  test("clicking a cell calls onSelect with that ISO date", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    render(<MonthCalendarGrid {...baseProps} onSelect={onSelect} />);
    await user.click(screen.getByTestId("month-grid-cell-2026-05-20"));
    expect(onSelect).toHaveBeenCalledWith("2026-05-20");
  });

  test("renders Korean weekday header row Sun-first", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const header = screen.getByTestId("month-grid-weekday-header");
    expect(header.textContent).toBe("일월화수목금토");
  });

  test("each cell carries the calendar-grid-cell class for media-query rules", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const cell = screen.getByTestId("month-grid-cell-2026-05-13");
    expect(cell).toHaveClass("calendar-grid-cell");
  });

  test("cells expose aria-label and aria-current for screen readers", () => {
    render(<MonthCalendarGrid {...baseProps} onSelect={() => {}} />);
    const today = screen.getByTestId("month-grid-cell-2026-05-11");
    expect(today.getAttribute("aria-current")).toBe("date");
    // aria-label includes year/month/day in Korean and the count.
    expect(today.getAttribute("aria-label")).toMatch(/2026.*5.*11.*3/);
  });

  test("count badge renders 많음 for any count >= 1000 (no +999 raw label)", () => {
    render(
      <MonthCalendarGrid
        {...baseProps}
        countByDate={new Map([["2026-05-13", 1234]])}
        onSelect={() => {}}
      />,
    );
    const cell = screen.getByTestId("month-grid-cell-2026-05-13");
    expect(cell).toHaveTextContent("많음");
    expect(cell.textContent ?? "").not.toContain("+999");
  });

  test("density='compact' stamps a data-density attribute on the root for media-query targeting", () => {
    render(<MonthCalendarGrid {...baseProps} density="compact" onSelect={() => {}} />);
    expect(screen.getByTestId("month-grid")).toHaveAttribute("data-density", "compact");
  });

  test("loading=true renders 42 skeleton cells with no count badges", () => {
    render(<MonthCalendarGrid {...baseProps} loading onSelect={() => {}} />);
    const skeletons = screen.getAllByTestId(/^month-grid-cell-skeleton-/);
    expect(skeletons).toHaveLength(42);
    expect(screen.queryByText("327")).not.toBeInTheDocument();
  });
});
