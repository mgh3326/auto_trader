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
      screen.getByRole("region", { name: "실행 리뷰" }),
    ).toBeInTheDocument();
    expect(screen.getByText(/자문 \/ 읽기 전용/)).toBeInTheDocument();
    expect(screen.getByText(/실주문 실행 없음/)).toBeInTheDocument();
    expect(
      screen.getAllByText(/명시적인 운영자 승인이 필요/).length,
    ).toBeGreaterThan(0);
    expect(screen.getByText(/실행 비활성화/)).toBeInTheDocument();
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
    // "Approval required" stage label (fixture) + "승인 필요" basket-line cell.
    expect(screen.getByText(/approval required/i)).toBeInTheDocument();
    expect(screen.getAllByText("승인 필요").length).toBeGreaterThan(0);
    // "Basket preview" stage label (fixture) + "바스켓 미리보기" section header.
    expect(screen.getAllByText(/basket preview/i).length).toBeGreaterThan(0);
    expect(screen.getAllByText("바스켓 미리보기").length).toBeGreaterThan(0);

    expect(screen.getAllByText("준비 완료").length).toBeGreaterThan(0);
    expect(screen.getAllByText("미사용").length).toBeGreaterThan(0);
  });

  it("renders basket preview lines when present", () => {
    render(<ExecutionReviewPanel review={makePreopenExecutionReview()} />);

    expect(screen.getByText("005930")).toBeInTheDocument();
    // side rendered as Korean "매수" in basket lines.
    expect(screen.getAllByText("매수").length).toBeGreaterThan(0);
    // account_mode rendered through EXECUTION_ACCOUNT_MODE_LABEL.
    expect(screen.getByText(/DB 시뮬레이션/)).toBeInTheDocument();
    expect(screen.getByText("70000")).toBeInTheDocument();
    expect(screen.getByText("10")).toBeInTheDocument();
    // Per-line guard rendered as Korean "승인 필요".
    expect(screen.getAllByText("승인 필요").length).toBeGreaterThan(0);
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
