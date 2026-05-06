// frontend/trading-decision/src/__tests__/RetrospectivePage.test.tsx
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";

vi.mock("../api/researchRetrospective", () => ({
  getRetrospectiveOverview: vi.fn(),
  getRetrospectiveStagePerformance: vi.fn(),
  listRetrospectiveDecisions: vi.fn(),
}));

import * as api from "../api/researchRetrospective";
import RetrospectivePage from "../pages/RetrospectivePage";

const baseOverview = {
  period_start: "2026-04-06T00:00:00Z",
  period_end: "2026-05-06T00:00:00Z",
  market: null,
  strategy: null,
  sessions_total: 5,
  summaries_total: 7,
  decision_distribution: {
    ai_buy: 2,
    ai_hold: 3,
    ai_sell: 2,
    user_accept: 1,
    user_reject: 1,
    user_modify: 0,
    user_defer: 0,
    user_pending: 5,
  },
  stage_coverage: [
    {
      stage_type: "market" as const,
      coverage_pct: 100,
      stale_pct: 0,
      unavailable_pct: 0,
    },
    {
      stage_type: "news" as const,
      coverage_pct: 60,
      stale_pct: 30,
      unavailable_pct: 10,
    },
    {
      stage_type: "fundamentals" as const,
      coverage_pct: 30,
      stale_pct: 0,
      unavailable_pct: 70,
    },
    {
      stage_type: "social" as const,
      coverage_pct: 0,
      stale_pct: 0,
      unavailable_pct: 100,
    },
  ],
  pnl: {
    realized_pnl_pct_avg: 1.2,
    unrealized_pnl_pct_avg: -0.5,
    sample_size: 6,
  },
  warnings: [],
};

afterEach(() => {
  cleanup();
  vi.resetAllMocks();
});

describe("RetrospectivePage", () => {
  beforeEach(() => {
    (api.getRetrospectiveOverview as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(baseOverview);
    (api.getRetrospectiveStagePerformance as unknown as ReturnType<typeof vi.fn>).mockResolvedValue([
      {
        stage_combo: "market+news",
        sample_size: 4,
        win_rate_pct: 75,
        avg_realized_pnl_pct: 2.1,
      },
    ]);
    (api.listRetrospectiveDecisions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      total: 1,
      rows: [
        {
          research_session_id: 11,
          symbol: "005930",
          market: "KR",
          decided_at: "2026-05-01T00:00:00Z",
          ai_decision: "buy",
          user_response: "accept",
          realized_pnl_pct: 3.4,
          proposal_id: 22,
        },
      ],
    });
  });

  it("renders overview cards and the decision drill-down row", async () => {
    render(<RetrospectivePage />);
    await waitFor(() => {
      expect(screen.getByText(/Research Retrospective/)).toBeInTheDocument();
    });
    // sessions_total card
    expect(await screen.findByText("5")).toBeInTheDocument();
    expect(screen.getByText(/1.20%/)).toBeInTheDocument();
    // decision drill-down
    expect(await screen.findByText("005930")).toBeInTheDocument();
    expect(screen.getByText(/3.40%/)).toBeInTheDocument();
    // session drill-down link
    const link = screen.getByRole("link", { name: /Session 열기/ });
    expect(link).toHaveAttribute(
      "href",
      "/trading/decisions/research/sessions/11/summary",
    );
  });

  it("renders empty warning when sessions_total is 0", async () => {
    (api.getRetrospectiveOverview as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      ...baseOverview,
      sessions_total: 0,
      summaries_total: 0,
      stage_coverage: [],
      pnl: {
        realized_pnl_pct_avg: null,
        unrealized_pnl_pct_avg: null,
        sample_size: 0,
      },
      warnings: ["no_research_summaries_in_window"],
    });
    (api.listRetrospectiveDecisions as unknown as ReturnType<typeof vi.fn>).mockResolvedValue({
      total: 0,
      rows: [],
    });

    render(<RetrospectivePage />);
    expect(
      await screen.findByText(/Research Summary가 없습니다/),
    ).toBeInTheDocument();
  });
});
