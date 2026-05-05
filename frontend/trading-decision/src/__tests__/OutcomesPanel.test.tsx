import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import OutcomesPanel from "../components/OutcomesPanel";
import { makeOutcome } from "../test/fixtures";

describe("OutcomesPanel", () => {
  it("shows an empty state when no outcomes are recorded", () => {
    render(<OutcomesPanel outcomes={[]} />);
    expect(screen.getByText(/결과 마크가 없습니다/)).toBeInTheDocument();
  });

  it("renders pnl_pct in the cell for the matching track and horizon", () => {
    const outcomes = [
      makeOutcome({
        track_kind: "accepted_live",
        horizon: "1h",
        pnl_pct: "2.5000",
      }),
      makeOutcome({
        id: 101,
        track_kind: "rejected_counterfactual",
        counterfactual_id: 11,
        horizon: "1d",
        pnl_pct: "-0.7500",
      }),
    ];
    render(<OutcomesPanel outcomes={outcomes} />);
    expect(
      screen.getByRole("table", { name: "결과 마크" }),
    ).toBeInTheDocument();
    expect(screen.getByText("2.5%")).toBeInTheDocument();
    expect(screen.getByText("-0.75%")).toBeInTheDocument();
  });
});
