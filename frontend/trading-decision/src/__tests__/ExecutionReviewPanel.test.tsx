import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import ExecutionReviewPanel from "../components/ExecutionReviewPanel";
import {
  makePreopenExecutionReview,
  makePreopenExecutionReviewUnavailable,
} from "../test/fixtures/preopen";

describe("ExecutionReviewPanel", () => {
  it("renders nothing when review is null", () => {
    const { container } = render(<ExecutionReviewPanel review={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders guardrail banner with advisory copy and execution disabled", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    expect(
      screen.getByRole("region", { name: /execution review/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/advisory.*read[- ]only/i)).toBeInTheDocument();
    expect(screen.getByText(/no live execution/i)).toBeInTheDocument();
    // This text appears in guardrail and stage summary.
    expect(
      screen.getAllByText(/requires later explicit operator approval/i).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(/Execution disabled/i)).toBeInTheDocument();
  });

  it("renders all six stages with their statuses", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    for (const label of [
      /data \/ news readiness/i,
      /candidate review/i,
      /cash \/ holdings \/ quotes/i,
      /post-order reconciliation/i,
    ]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // "Approval required" appears as a stage label and inside basket lines.
    expect(screen.getAllByText(/approval required/i).length).toBeGreaterThan(1);
    // "Basket preview" appears multiple times (stage label and section header).
    expect(screen.getAllByText(/basket preview/i).length).toBeGreaterThan(1);

    expect(screen.getAllByText(/ready/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/unavailable/i).length).toBeGreaterThan(0);
  });

  it("renders basket preview lines when present", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    expect(screen.getByText("005930")).toBeInTheDocument();
    // side "buy" appears in multiple places; use getAll or be specific.
    expect(screen.getAllByText(/buy/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/db_simulated/i)).toBeInTheDocument();
    expect(screen.getByText("70000")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    // Per-line guard rendered.
    expect(screen.getAllByText(/approval required/i).length).toBeGreaterThan(0);
  });

  it("hides basket preview block when basket is null and shows degraded copy", () => {
    render(
      <ExecutionReviewPanel review={makePreopenExecutionReviewUnavailable()} />,
    );

    // This summary appears for multiple stages when run is unavailable.
    expect(
      screen.getAllByText(/no open preopen research run/i).length,
    ).toBeGreaterThan(0);
    expect(screen.queryByText("005930")).toBeNull();
  });

  it("renders blocking reasons as warning chips", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);
    // "mvp_read_only" appears both in basket warnings and execution blocking reasons.
    expect(screen.getAllByText(/mvp_read_only/).length).toBeGreaterThan(0);
  });
});
