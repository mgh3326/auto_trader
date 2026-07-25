import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { RetrospectiveCard } from "../desktop/stock-detail/RetrospectiveCard";
import type { RetrospectiveRow } from "../types/retrospectives";

function retro(overrides: Partial<RetrospectiveRow> = {}): RetrospectiveRow {
  return {
    id: 1, correlation_id: "c", symbol: "005930", market: "kr",
    instrument_type: "equity_kr", side: "buy", trigger_type: "fill",
    root_cause_class: "analysis", outcome: "win", realized_pnl: 1000,
    realized_pnl_currency: "KRW", pnl_pct: 2.5, result_summary: null,
    lesson: "분할 매수가 유효했다", next_strategy: null,
    intended_vs_happened: null,
    next_actions: [{ action: "재진입 룰 재검토", status: "open" }],
    guardrail_fired: null, policy_version: null,
    created_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

describe("RetrospectiveCard", () => {
  test("shows loading state when undefined", () => {
    render(<RetrospectiveCard retrospectives={undefined} />);
    expect(screen.getByText("불러오는 중입니다…")).toBeInTheDocument();
  });

  test("shows empty state when no retrospectives", () => {
    render(<RetrospectiveCard retrospectives={[]} />);
    expect(screen.getByText("등록된 회고가 없습니다.")).toBeInTheDocument();
  });

  test("renders lesson, trigger and incomplete next action", () => {
    render(<RetrospectiveCard retrospectives={[retro()]} />);
    expect(screen.getByText(/분할 매수가 유효했다/)).toBeInTheDocument();
    expect(screen.getByText("fill")).toBeInTheDocument();
    expect(screen.getByText("재진입 룰 재검토")).toBeInTheDocument();
  });

  test("hides done next actions", () => {
    render(
      <RetrospectiveCard
        retrospectives={[retro({ next_actions: [{ action: "끝난 액션", status: "done" }] })]}
      />,
    );
    expect(screen.queryByText("끝난 액션")).not.toBeInTheDocument();
  });

  test("ROB-885: shows open and in_progress actions (explicit allowlist)", () => {
    render(
      <RetrospectiveCard
        retrospectives={[
          retro({
            next_actions: [
              { action: "열린 액션", status: "open" },
              { action: "진행 액션", status: "in_progress" },
            ],
          }),
        ]}
      />,
    );
    expect(screen.getByText("열린 액션")).toBeInTheDocument();
    expect(screen.getByText("진행 액션")).toBeInTheDocument();
  });

  test("ROB-885: hides obsolete, expired, missing-status, and unknown-status actions", () => {
    render(
      <RetrospectiveCard
        retrospectives={[
          retro({
            next_actions: [
              { action: "폐기 액션", status: "obsolete" },
              { action: "만료 액션", status: "expired" },
              { action: "상태누락 액션" },
              { action: "알수없음 액션", status: "bogus" },
              { action: "보여줘", status: "open" },
            ],
          }),
        ]}
      />,
    );
    expect(screen.queryByText("폐기 액션")).not.toBeInTheDocument();
    expect(screen.queryByText("만료 액션")).not.toBeInTheDocument();
    expect(screen.queryByText("상태누락 액션")).not.toBeInTheDocument();
    expect(screen.queryByText("알수없음 액션")).not.toBeInTheDocument();
    expect(screen.getByText("보여줘")).toBeInTheDocument();
  });
});
