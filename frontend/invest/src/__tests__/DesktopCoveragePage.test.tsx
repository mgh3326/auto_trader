import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import * as benchmarkGapApi from "../api/benchmarkGap";
import * as coverageApi from "../api/coverage";
import { CoverageRoute } from "../pages/desktop/DesktopCoveragePage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type { BenchmarkGapMatrixResponse } from "../types/benchmarkGap";
import type { InvestCoverageResponse } from "../types/coverage";

const BASE_ACTIONABILITY = {
  priority: "none" as const,
  action: "monitor" as const,
  queue: "none",
  approvalGates: [],
  reason: "fresh",
  safeByDefault: true,
};

const COVERAGE_PAYLOAD: InvestCoverageResponse = {
  market: "all",
  asOf: "2026-05-11T08:00:00Z",
  tradingDate: "2026-05-11",
  states: ["fresh", "provider_unwired", "unsupported"],
  surfaces: [
    {
      surface: "news_feed",
      label: "News feed",
      market: "all",
      state: "fresh",
      sourceOfTruth: "news_ingestor",
      references: ["toss", "naver"],
      latestAt: "2026-05-11T08:00:00Z",
      staleAfterHours: 24,
      counts: { fresh: 3, stale: 0, missing: 0, partial: 0, total: 3 },
      warnings: [],
      notes: [],
      sourceCandidates: [
        {
          name: "naver_finance",
          surface: "news_feed",
          kind: "candidate",
          readiness: "request_time_only",
          latestAt: null,
          counts: null,
          warnings: [],
          notes: ["reference only"],
        },
      ],
      actionability: BASE_ACTIONABILITY,
    },
    {
      surface: "quotes",
      label: "Quotes",
      market: "all",
      state: "provider_unwired",
      sourceOfTruth: "provider_contract",
      references: ["toss", "naver"],
      latestAt: null,
      staleAfterHours: null,
      counts: { fresh: 0, stale: 0, missing: 1, partial: 0, total: 1 },
      warnings: ["durable read model missing"],
      notes: [],
      sourceCandidates: [],
      actionability: {
        priority: "blocked",
        action: "provider_contract_needed",
        queue: "provider-contract",
        approvalGates: ["code_review"],
        reason: "contract needed",
        safeByDefault: true,
      },
    },
  ],
  symbols: [
    {
      symbol: "005930",
      market: "kr",
      surfaces: { screener_snapshots: "fresh", naver_investor_flow: "fresh" },
      latestDates: { screener_snapshots: "2026-05-11", naver_investor_flow: "2026-05-11" },
      warnings: [],
      actionability: BASE_ACTIONABILITY,
    },
    {
      symbol: "AAPL",
      market: "us",
      surfaces: { screener_snapshots: "fresh", investor_flow: "unsupported" },
      latestDates: { screener_snapshots: "2026-05-11", investor_flow: null },
      warnings: ["investor_flow"],
      actionability: BASE_ACTIONABILITY,
    },
    {
      symbol: "MSFT",
      market: "us",
      surfaces: { screener_snapshots: "missing", naver_investor_flow: "unsupported" },
      latestDates: { screener_snapshots: null, naver_investor_flow: null },
      warnings: ["screener_snapshots"],
      actionability: {
        priority: "high",
        action: "backfill_candidate",
        queue: "invest-data-read-models",
        approvalGates: ["production_db_write_approval", "scheduler_activation_approval"],
        reason: "candidate only",
        safeByDefault: true,
      },
    },
  ],
  gaps: [],
  notes: ["read-only coverage dashboard"],
};

const BENCHMARK_GAP_PAYLOAD: BenchmarkGapMatrixResponse = {
  market: "kr",
  asOf: "2026-05-19T00:00:00Z",
  rows: [],
  nextCandidates: [],
  summary: { totalRows: 0, byStatus: {}, byPriority: {}, byProvider: {} },
  sourcePolicy: ["KIS live = broker authority"],
  notes: [],
};

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/coverage"]}>{ui}</MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.spyOn(benchmarkGapApi, "fetchBenchmarkGapMatrix").mockResolvedValue(BENCHMARK_GAP_PAYLOAD);
  vi.spyOn(coverageApi, "fetchInvestCoverage").mockResolvedValue(COVERAGE_PAYLOAD);
});

test("renders coverage actionability, candidate readiness, and all-market symbols", async () => {
  render(wrap(<CoverageRoute />));

  await waitFor(() => expect(screen.getByRole("heading", { name: "데이터 커버리지" })).toBeInTheDocument());
  await waitFor(() => expect(screen.getByText(/naver_finance/)).toBeInTheDocument());

  expect(screen.getByText(/request-time/)).toBeInTheDocument();
  expect(screen.getByText(/provider-contract/)).toBeInTheDocument();
  expect(screen.getByText(/code_review/)).toBeInTheDocument();
  expect(screen.getByText("005930")).toBeInTheDocument();
  expect(screen.getByText("AAPL")).toBeInTheDocument();
  expect(screen.getByText("MSFT")).toBeInTheDocument();
  expect(screen.getByText(/production_db_write_approval/)).toBeInTheDocument();
  expect(screen.getByText(/advisory metadata/)).toBeInTheDocument();
});
