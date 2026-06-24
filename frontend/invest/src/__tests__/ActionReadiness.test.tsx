import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { fetchKrActionReadiness } from "../api/actionReadiness";
import { CoverageRoute } from "../pages/desktop/DesktopCoveragePage";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";

const actionability = {
  priority: "none",
  action: "none",
  queue: null,
  approvalGates: [],
  reason: null,
  safeByDefault: true,
};

const counts = {
  expected: null,
  fresh: 1,
  stale: 0,
  missing: 0,
  partial: 0,
  total: 1,
};

function coverageResponse() {
  return {
    market: "kr",
    asOf: "2026-05-14T09:00:00Z",
    tradingDate: "2026-05-14",
    states: ["fresh", "missing", "provider_unwired"],
    surfaces: [
      {
        surface: "quotes",
        label: "quotes",
        state: "fresh",
        market: "kr",
        sourceOfTruth: "market_quote_snapshots",
        references: ["toss"],
        latestAt: "2026-05-14T09:00:00Z",
        latestDate: null,
        counts,
        staleAfterHours: null,
        warnings: [],
        notes: [],
        sourceCandidates: [],
        actionability,
      },
    ],
    symbols: [],
    gaps: [],
    notes: [],
  };
}

function readinessResponse() {
  return {
    market: "kr",
    asOf: "2026-05-14T09:00:00Z",
    symbol: "005930",
    overallState: "blocked",
    canGenerateBuyReport: false,
    canGenerateSellReport: false,
    blockers: ["kis_live_cash_orderable: KIS live 주문가능 현금 확인 불가"],
    degradedSignals: ["investor_flow: stale 상태입니다."],
    sourcePolicy: [
      "KIS live broker values are authoritative for tradeable KR holdings, cash, open orders, and sellable quantity.",
      "/invest DB/read-model state is the product authority for market, screener, Naver/Toss-derived reference, news, calendar, valuation, flow, and historical ledger readiness.",
      "Toss/Naver/external sources are displayed only as reference, candidate, or supporting signals and are never source-of-truth for action readiness.",
    ],
    notes: ["Read-only readiness only: no order, watch/order-intent, scheduler, backfill, or broker mutation is performed."],
    families: [
      {
        key: "kis_live_cash_orderable",
        labelKo: "KIS live 주문가능 현금",
        category: "Broker authority",
        state: "blocked",
        impact: "blocks_buy_report",
        authority: "kis_live_broker",
        sourceOfTruth: "KIS live via existing InvestHomeService/account-panel",
        references: ["manual_or_paper_reference"],
        latestAt: null,
        latestDate: null,
        counts: null,
        coverageState: null,
        actionability: { ...actionability, priority: "blocked", action: "provider_contract_needed", queue: "invest-action-readiness-review", approvalGates: ["code_review"], reason: "KIS live 주문가능 현금 확인 불가" },
        blockers: ["KIS live 주문가능 현금 확인 불가"],
        warnings: [],
        notes: ["Existing InvestHomeService/account-panel read path only; no new broker mutation path."],
        links: [],
      },
      {
        key: "investor_flow",
        labelKo: "투자자 수급",
        category: "Market/read-model data",
        state: "degraded",
        impact: "degrades_report",
        authority: "auto_trader_read_model",
        sourceOfTruth: "investor_flow_snapshots",
        references: ["naver"],
        latestAt: null,
        latestDate: "2026-05-13",
        counts,
        coverageState: "stale",
        actionability: { ...actionability, priority: "high", action: "investigate", queue: "invest-action-readiness-review", approvalGates: ["code_review"], reason: "stale" },
        blockers: [],
        warnings: ["investor_flow: stale 상태입니다."],
        notes: [],
        links: [],
      },
    ],
  };
}

// CoverageRoute also fetches the benchmark-gap matrix (ROB-271). This test only
// needs the KR action-readiness card, so an empty-but-well-formed matrix keeps
// <BenchmarkGapSection> from crashing without asserting anything about it.
function benchmarkGapResponse() {
  return {
    market: "kr",
    asOf: "2026-05-14T09:00:00Z",
    rows: [],
    nextCandidates: [],
    summary: { totalRows: 0, byStatus: {}, byPriority: {}, byProvider: {} },
    sourcePolicy: [],
    notes: [],
  };
}

const fetchMock = vi.fn();
beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  fetchMock.mockReset();
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    // CoverageRoute fires a third fetch to /invest/api/coverage/benchmark-gap
    // (ROB-271). It must be matched BEFORE the generic /invest/api/coverage branch
    // (more-specific prefix first) — otherwise it resolves to a rows-less coverage
    // shape and <BenchmarkGapSection> crashes the whole page on `rows.filter`.
    if (url.startsWith("/invest/api/coverage/benchmark-gap")) {
      return Promise.resolve({ ok: true, json: async () => benchmarkGapResponse() });
    }
    if (url.startsWith("/invest/api/coverage")) {
      return Promise.resolve({ ok: true, json: async () => coverageResponse() });
    }
    if (url.startsWith("/invest/api/kr/action-readiness")) {
      return Promise.resolve({ ok: true, json: async () => readinessResponse() });
    }
    return Promise.resolve({ ok: false, status: 404, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("action readiness API client builds symbol query and includes credentials", async () => {
  await fetchKrActionReadiness({ symbol: " 005930 " });

  expect(fetchMock).toHaveBeenCalledWith(
    "/invest/api/kr/action-readiness?symbol=005930",
    expect.objectContaining({ credentials: "include" }),
  );
});

test("coverage page renders KR action readiness blockers and source boundaries without execution buttons", async () => {
  render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/coverage"]}>
        <CoverageRoute />
      </MemoryRouter>
    </AccountPanelProvider>,
  );

  expect(await screen.findByText("KR 액션 리포트 준비도")).toBeInTheDocument();
  expect(screen.getByText("매수 리포트 차단")).toBeInTheDocument();
  expect(screen.getByText("매도 리포트 차단")).toBeInTheDocument();
  expect(screen.getAllByText(/KIS live 주문가능 현금 확인 불가/).length).toBeGreaterThan(0);
  expect(screen.getByText(/authority KIS live/)).toBeInTheDocument();
  expect(screen.getByText(/sourceOfTruth: investor_flow_snapshots/)).toBeInTheDocument();
  expect(screen.getByText(/Toss\/Naver\/external sources/)).toBeInTheDocument();

  await waitFor(() => {
    expect(fetchMock).toHaveBeenCalledWith(
      "/invest/api/kr/action-readiness?symbol=005930",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  expect(screen.queryByRole("button", { name: /주문|백필|스케줄|실행|refresh/i })).not.toBeInTheDocument();
});
