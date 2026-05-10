import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, test } from "vitest";
import { CalendarSourceButton } from "../components/calendar/CalendarSourceButton";
import type { CalendarSourceStatus } from "../types/calendar";

function src(overrides: Partial<CalendarSourceStatus>): CalendarSourceStatus {
  return {
    source: "finnhub", category: "earnings", market: "us", state: "fresh",
    lastSuccessAt: null, lastFailureAt: null, lastError: null,
    succeededPartitions: 0, failedPartitions: 0, missingPartitions: 0, eventCount: 0,
    ...overrides,
  };
}

describe("CalendarSourceButton (ROB-185)", () => {
  test("renders quiet button labelled '데이터 출처'", () => {
    render(<CalendarSourceButton sources={[src({ state: "fresh" })]} />);
    const btn = screen.getByTestId("calendar-source-button");
    expect(btn).toHaveTextContent("데이터 출처");
    // No banner element should ever appear regardless of source states.
    expect(screen.queryByTestId("calendar-freshness-banner")).not.toBeInTheDocument();
  });

  test("popover is hidden by default", () => {
    render(<CalendarSourceButton sources={[src({ state: "fresh" })]} />);
    expect(screen.queryByTestId("calendar-source-popover")).not.toBeInTheDocument();
  });

  test("clicking the button opens the popover with one row per source", async () => {
    const user = userEvent.setup();
    render(
      <CalendarSourceButton
        sources={[
          src({ source: "finnhub", state: "fresh" }),
          src({ source: "dart", state: "stale" }),
        ]}
      />,
    );
    await user.click(screen.getByTestId("calendar-source-button"));
    const pop = screen.getByTestId("calendar-source-popover");
    // Two rows.
    expect(pop.querySelectorAll('[data-testid="calendar-source-row"]')).toHaveLength(2);
    // Friendly Korean labels, not source ids.
    expect(pop).toHaveTextContent("미국 실적 일정");
    expect(pop).toHaveTextContent("한국 공시");
    expect(pop).not.toHaveTextContent("finnhub");
    expect(pop).not.toHaveTextContent("dart");
    expect(pop).not.toHaveTextContent("ForexFactory");
  });

  test("stale source row shows the friendly stale copy, fresh row does not", async () => {
    const user = userEvent.setup();
    render(
      <CalendarSourceButton
        sources={[
          src({ source: "finnhub", state: "fresh" }),
          src({ source: "dart", state: "stale" }),
        ]}
      />,
    );
    await user.click(screen.getByTestId("calendar-source-button"));
    const rows = screen.getAllByTestId("calendar-source-row");
    const dartRow = rows.find((r) => r.textContent?.includes("한국 공시"))!;
    expect(dartRow).toHaveTextContent("방금 업데이트되지 않았어요");
    const finnRow = rows.find((r) => r.textContent?.includes("미국 실적 일정"))!;
    expect(finnRow).not.toHaveTextContent("방금");
  });

  test("button has aria-expanded reflecting popover state", async () => {
    const user = userEvent.setup();
    render(<CalendarSourceButton sources={[src({ state: "fresh" })]} />);
    const btn = screen.getByTestId("calendar-source-button");
    expect(btn).toHaveAttribute("aria-expanded", "false");
    await user.click(btn);
    expect(btn).toHaveAttribute("aria-expanded", "true");
  });

  test("empty sources list still renders the button (silent passthrough)", () => {
    render(<CalendarSourceButton sources={[]} />);
    expect(screen.getByTestId("calendar-source-button")).toBeInTheDocument();
  });

  test("the default DOM contains no banished operational strings", () => {
    render(
      <CalendarSourceButton
        sources={[src({ source: "finnhub", state: "stale" })]}
      />,
    );
    const body = document.body.textContent ?? "";
    for (const bad of [
      "데이터 상태:",
      "오래됨",
      "수집 실패",
      "미수집",
      "Finnhub",
      "DART",
      "ForexFactory",
    ]) {
      expect(body).not.toContain(bad);
    }
  });
});
