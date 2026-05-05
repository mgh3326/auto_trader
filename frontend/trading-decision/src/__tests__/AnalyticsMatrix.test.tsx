import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import AnalyticsMatrix from "../components/AnalyticsMatrix";
import { makeAnalyticsCell, makeAnalyticsResponse } from "../test/fixtures";

describe("AnalyticsMatrix", () => {
  it("shows an empty state when no cells exist", () => {
    render(<AnalyticsMatrix data={makeAnalyticsResponse({ cells: [] })} />);
    expect(screen.getByText(/아직 결과가 없습니다/)).toBeInTheDocument();
  });

  it("renders one cell for each (track, horizon) row from the response", () => {
    const data = makeAnalyticsResponse({
      cells: [
        makeAnalyticsCell({
          track_kind: "accepted_live",
          horizon: "1h",
          mean_pnl_pct: "1.5",
          outcome_count: 3,
        }),
        makeAnalyticsCell({
          track_kind: "rejected_counterfactual",
          horizon: "1d",
          mean_pnl_pct: "-0.5",
          outcome_count: 1,
        }),
      ],
    });
    render(<AnalyticsMatrix data={data} />);
    expect(screen.getByRole("table", { name: "결과 분석" })).toBeInTheDocument();
    expect(screen.getByText("1.5%")).toBeInTheDocument();
    expect(screen.getByText("-0.5%")).toBeInTheDocument();
    expect(screen.getAllByText(/n=/i).length).toBeGreaterThanOrEqual(2);
  });
});
