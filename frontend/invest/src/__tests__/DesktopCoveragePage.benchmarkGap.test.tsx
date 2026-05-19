import type { ReactElement } from "react";
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

const COVERAGE_EMPTY: InvestCoverageResponse = {
  market: "kr",
  asOf: "2026-05-19T00:00:00Z",
  tradingDate: "2026-05-19",
  states: ["fresh"],
  surfaces: [],
  symbols: [],
  gaps: [],
  notes: [],
};

const GAP_PAYLOAD: BenchmarkGapMatrixResponse = {
  market: "kr",
  asOf: "2026-05-19T00:00:00Z",
  rows: [
    {
      id: "toss.screener",
      featureArea: "screener",
      benchmarkProvider: "toss",
      benchmarkSurface: "screener.presets",
      benchmarkLabelKo: "골라보기",
      sourceRole: "benchmark_only",
      coverageStatus: "partial",
      priority: "P2",
      whyNeeded: "screener parity",
      nextAction: "map presets",
      newIssueCandidate: false,
      notes: [],
    },
    {
      id: "naver.market.kr",
      featureArea: "market",
      benchmarkProvider: "naver",
      benchmarkSurface: "market.kr",
      benchmarkLabelKo: "국내 시장",
      sourceRole: "reference",
      coverageStatus: "covered",
      priority: "P2",
      whyNeeded: "kr market parity",
      nextAction: "monitor",
      newIssueCandidate: false,
      notes: [],
    },
  ],
  nextCandidates: [
    {
      rowId: "toss.screener",
      priority: "P2",
      featureArea: "screener",
      benchmarkProvider: "toss",
      gap: "missing toss-style presets",
      currentAutoTrader: "/invest/api/screener/presets",
      whyItMatters: "parity baseline",
      currentStatus: "partial",
      nextAction: "map presets",
      newIssueCandidate: false,
    },
  ],
  summary: { totalRows: 2, byStatus: { partial: 1, covered: 1 }, byPriority: { P2: 2 }, byProvider: { toss: 1, naver: 1 } },
  sourcePolicy: ["Toss = benchmark/reference only — never sourceOfTruth"],
  notes: ["first-screen view"],
};

function wrap(ui: ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/coverage"]}>{ui}</MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.spyOn(coverageApi, "fetchInvestCoverage").mockResolvedValue(COVERAGE_EMPTY);
  vi.spyOn(benchmarkGapApi, "fetchBenchmarkGapMatrix").mockResolvedValue(GAP_PAYLOAD);
});

test("benchmark gap section is the first visible section after header/filter", async () => {
  render(wrap(<CoverageRoute />));
  await waitFor(() =>
    expect(screen.getByText(/토스·네이버 대비 데이터 수급 현황/)).toBeInTheDocument(),
  );
  expect(screen.getByText(/다음 수급 후보/)).toBeInTheDocument();
  expect(screen.getByText(/Toss benchmark/)).toBeInTheDocument();
  expect(screen.getByText(/Naver benchmark/)).toBeInTheDocument();
  expect(screen.getByText(/auto_trader 내부/)).toBeInTheDocument();
  // legacy panels live under collapsed details
  expect(screen.getByText(/KR 액션 리포트 준비도 \(보조\)/)).toBeInTheDocument();
  expect(screen.getByText(/개발자 · 디버그 raw 커버리지/)).toBeInTheDocument();
  // source policy is rendered
  expect(screen.getByText(/Toss = benchmark/)).toBeInTheDocument();
});
