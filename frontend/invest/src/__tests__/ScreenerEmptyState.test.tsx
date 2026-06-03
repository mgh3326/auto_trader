import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ScreenerEmptyState } from "../desktop/screener/ScreenerEmptyState";

describe("ScreenerEmptyState", () => {
  it("renders neutral copy for healthy_no_matches (not a warning)", () => {
    render(<ScreenerEmptyState reason="healthy_no_matches" coverageLabel={null} />);
    expect(screen.getByText(/조건에 맞는 종목이 없습니다/)).toBeInTheDocument();
  });

  it("renders the coverage label for coverage_below_floor", () => {
    render(
      <ScreenerEmptyState reason="coverage_below_floor" coverageLabel="20 / 3,800 (0.5%)" />,
    );
    expect(screen.getByText(/20 \/ 3,800 \(0\.5%\)/)).toBeInTheDocument();
  });

  it("renders snapshot_missing copy", () => {
    render(<ScreenerEmptyState reason="snapshot_missing" coverageLabel={null} />);
    expect(screen.getByText(/스냅샷.*준비/)).toBeInTheDocument();
  });

  it("falls back to a generic message when reason is null", () => {
    render(<ScreenerEmptyState reason={null} coverageLabel={null} />);
    expect(screen.getByText("표시할 종목이 없습니다.")).toBeInTheDocument();
  });
});
