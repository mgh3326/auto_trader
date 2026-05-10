import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test, vi } from "vitest";
import { CalendarMonthHeader } from "../components/calendar/CalendarMonthHeader";

describe("CalendarMonthHeader", () => {
  test("renders the title and prev/next buttons with stable test ids", () => {
    render(
      <CalendarMonthHeader
        title="2026년 5월"
        onPrev={() => {}}
        onNext={() => {}}
      />,
    );
    expect(screen.getByText("2026년 5월")).toBeInTheDocument();
    expect(screen.getByTestId("calendar-prev-month")).toHaveAttribute("aria-label", "이전 달");
    expect(screen.getByTestId("calendar-next-month")).toHaveAttribute("aria-label", "다음 달");
  });

  test("clicking prev/next fires the callbacks", async () => {
    const onPrev = vi.fn();
    const onNext = vi.fn();
    const user = userEvent.setup();
    render(<CalendarMonthHeader title="2026년 5월" onPrev={onPrev} onNext={onNext} />);
    await user.click(screen.getByTestId("calendar-prev-month"));
    await user.click(screen.getByTestId("calendar-next-month"));
    expect(onPrev).toHaveBeenCalledTimes(1);
    expect(onNext).toHaveBeenCalledTimes(1);
  });

  test("buttons opt into the calendar-nav-btn class for focus-ring rules", () => {
    render(<CalendarMonthHeader title="t" onPrev={() => {}} onNext={() => {}} />);
    expect(screen.getByTestId("calendar-prev-month")).toHaveClass("calendar-nav-btn");
    expect(screen.getByTestId("calendar-next-month")).toHaveClass("calendar-nav-btn");
  });
});
