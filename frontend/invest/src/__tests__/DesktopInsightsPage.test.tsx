import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { DesktopInsightsPage } from "../pages/desktop/DesktopInsightsPage";
import { useCommonPreferredDisparity } from "../hooks/useCommonPreferredDisparity";
import { useMarketParity } from "../hooks/useMarketParity";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";

vi.mock("../hooks/useMarketParity", () => ({ useMarketParity: vi.fn() }));
vi.mock("../hooks/useCommonPreferredDisparity", () => ({ useCommonPreferredDisparity: vi.fn() }));

const marketParityReady = {
  state: {
    status: "ready" as const,
    data: {
      asOf: "2026-05-14T00:00:00Z",
      market: "kr" as const,
      state: "fresh" as const,
      emptyReason: null,
      warnings: [],
      notes: ["read-only"],
      cards: [
        {
          id: "kospi-etf",
          title: "KOSPI ETF parity",
          type: "index_implied_parity" as const,
          baseSymbol: "KOSPI",
          baseName: "KOSPI",
          proxySymbol: "069500",
          syntheticSymbol: null,
          formula: "ETF/NAV",
          premiumPct: 1.23,
          tone: "premium" as const,
          dataState: "fresh" as const,
          emptyReason: null,
          source: {
            source: "read-only fixture",
            sourceOfTruth: "market parity service",
            asOf: "2026-05-14T00:00:00Z",
            freshnessSec: 60,
            stale: false,
            warnings: [],
          },
        },
      ],
    },
  },
  reload: vi.fn(),
};

const disparityReady = {
  status: "ready" as const,
  data: {
    asOf: "2026-05-14T00:00:00Z",
    market: "kr" as const,
    state: "fresh" as const,
    emptyReason: null,
    warnings: [],
    notes: ["read-only"],
    cards: [
      {
        id: "005930-005935",
        commonSymbol: "005930",
        commonName: "삼성전자",
        preferredSymbol: "005935",
        preferredName: "삼성전자우",
        commonPrice: 80000,
        preferredPrice: 65000,
        disparityPct: -18.75,
        zScore: -1.2,
        tone: "discount" as const,
        dataState: "fresh" as const,
        emptyReason: null,
        primaryWindow: "60d" as const,
        windows: [{ period: "60d" as const, sampleCount: 60, meanDisparityPct: -15, zScore: -1.2, dataState: "fresh" as const }],
        formula: "preferred/common - 1",
        source: {
          source: "read-only fixture",
          sourceOfTruth: "common preferred disparity service",
          asOf: "2026-05-14T00:00:00Z",
          stale: false,
          freshnessSec: 60,
          warnings: [],
        },
        caution: "매수·매도 추천이 아닙니다.",
        warnings: [],
      },
    ],
  },
};

function wrap(ui: React.ReactElement) {
  return (
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/insights"]}>{ui}</MemoryRouter>
    </AccountPanelProvider>
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.mocked(useMarketParity).mockReturnValue(marketParityReady);
  vi.mocked(useCommonPreferredDisparity).mockReturnValue(disparityReady);
});

afterEach(() => vi.unstubAllGlobals());

test("renders the dedicated read-only insights scaffold", () => {
  render(wrap(<DesktopInsightsPage />));

  expect(screen.getByRole("heading", { name: "인사이트" })).toBeInTheDocument();
  // ROB-677: the dev-facing "ROB-253 decision" eyebrow is gone from production UI
  expect(screen.queryByText(/ROB-253/)).not.toBeInTheDocument();
  // ROB-677: cards grouped into labelled sections
  expect(screen.getByRole("heading", { name: "시장 관찰" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "판단 품질" })).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "세션 기록" })).toBeInTheDocument();
  expect(screen.getByText(/주문·매매·watch mutation API를 호출하지 않습니다/)).toBeInTheDocument();
  expect(screen.getByText("KOSPI ETF parity")).toBeInTheDocument();
  expect(screen.getByText("삼성전자 / 삼성전자우")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "시장 대시보드" })).toHaveAttribute("href", "/invest/market");
  // ROB-678: retrospectives panel mounted under a 학습·회고 section
  expect(screen.getByRole("heading", { name: "학습·회고" })).toBeInTheDocument();
  expect(screen.getByTestId("retrospectives-panel")).toBeInTheDocument();
});

test("shows the accumulating banner when all three data panels are empty (ROB-677)", async () => {
  const fetchMock = vi.fn(async (url: string) => {
    const u = String(url);
    let body: unknown = {};
    // ROB-691: /scoreboard must be checked before the generic "retrospectives"
    // substring match below, since its URL also contains "retrospectives".
    if (u.includes("/scoreboard")) {
      body = {
        group_by: "strategy", market: "all", kst_date_from: null, kst_date_to: null,
        count: 0, groups: [], as_of: "2026-07-03T00:00:00Z",
        totals: { sample_size: 0, wins: 0, misses: 0, decided: 0, win_rate_pct: null, realized_pnl_sum: {}, fx_pnl_krw_sum: 0, total_pnl_krw_sum: 0, excluded_no_fill_evidence: 0 },
      };
    } else if (u.includes("/calibration")) {
      body = { group_by: "created_by", created_by: null, symbol: null, instrument_type: null, days: null, count: 0, groups: [], as_of: "2026-07-03T00:00:00Z" };
    } else if (u.includes("/forecasts/open") || u.includes("/forecasts/closed")) {
      body = { kind: "open", symbol: null, created_by: null, instrument_type: null, count: 0, items: [], as_of: "2026-07-03T00:00:00Z" };
    } else if (u.includes("/artifacts")) {
      body = { success: true, count: 0, filters: {}, artifacts: [] };
    } else if (u.includes("/session-context")) {
      body = { success: true, count: 0, filters: {}, entries: [] };
    } else if (u.includes("next-actions")) {
      body = { market: "all", symbol: null, count: 0, scan_limit: 200, items: [] };
    } else if (u.includes("retrospectives")) {
      body = { market: "all", trigger_type: null, root_cause_class: null, symbol: null, count: 0, total: 0, items: [], as_of: "2026-07-03T00:00:00Z" };
    }
    return { ok: true, status: 200, json: async () => body };
  });
  vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);

  render(wrap(<DesktopInsightsPage />));

  expect(
    await screen.findByText(/판단 품질·핸드오프 데이터는 아직 축적 중입니다/),
  ).toBeInTheDocument();
});

test("renders loading and error states for independent insight widgets", () => {
  vi.mocked(useMarketParity).mockReturnValue({ state: { status: "error", message: "boom" }, reload: vi.fn() });
  vi.mocked(useCommonPreferredDisparity).mockReturnValue({ status: "loading" });

  render(wrap(<DesktopInsightsPage />));

  expect(screen.getByText(/괴리 참고 카드를 일시적으로 불러오지 못했습니다/)).toBeInTheDocument();
  expect(screen.getByText(/보통주\/우선주 괴리 데이터를 불러오는 중/)).toBeInTheDocument();
});

test("renders an empty market parity state without hiding the page", () => {
  vi.mocked(useMarketParity).mockReturnValue({
    state: {
      status: "ready",
      data: { ...marketParityReady.state.data, state: "missing", emptyReason: "승인된 패리티 카드가 없습니다.", cards: [] },
    },
    reload: vi.fn(),
  });

  render(wrap(<DesktopInsightsPage />));

  expect(screen.getByText("승인된 패리티 카드가 없습니다.")).toBeInTheDocument();
  expect(screen.getByText("삼성전자 / 삼성전자우")).toBeInTheDocument();
});

// Builds a full-page fetch mock (calibration/open/closed/artifacts/
// session-context/next-actions/retrospectives) with one closed forecast
// (symbol AAPL, thesis-style correlation_id) and one retrospective whose
// symbol is the given `retroSymbol` (exec-style correlation_id). The two
// correlation_ids are always disjoint — under ROB-678's exact-id scheme the
// crosslink would never render regardless of symbol overlap.
function buildCrosslinkFetchMock(retroSymbol: string) {
  return vi.fn(async (url: string) => {
    const u = String(url);
    let body: unknown = {};
    // ROB-691: /scoreboard must be checked before the generic "retrospectives"
    // substring match below, since its URL also contains "retrospectives".
    if (u.includes("/scoreboard")) {
      body = {
        group_by: "strategy", market: "all", kst_date_from: null, kst_date_to: null,
        count: 0, groups: [], as_of: "2026-07-03T00:00:00Z",
        totals: { sample_size: 0, wins: 0, misses: 0, decided: 0, win_rate_pct: null, realized_pnl_sum: {}, fx_pnl_krw_sum: 0, total_pnl_krw_sum: 0, excluded_no_fill_evidence: 0 },
      };
    } else if (u.includes("/calibration")) {
      body = { group_by: "created_by", created_by: null, symbol: null, instrument_type: null, days: null, count: 0, groups: [], as_of: "2026-07-03T00:00:00Z" };
    } else if (u.includes("/forecasts/open")) {
      body = { kind: "open", symbol: null, created_by: null, instrument_type: null, count: 0, items: [], as_of: "2026-07-03T00:00:00Z" };
    } else if (u.includes("/forecasts/closed")) {
      body = {
        kind: "closed", symbol: null, created_by: null, instrument_type: null, count: 1, as_of: "2026-07-03T00:00:00Z",
        items: [{
          id: 1, forecast_id: "f1", correlation_id: "aapl-thesis", created_by: "claude",
          session_label: null, model_label: null, symbol: "AAPL", instrument_type: "equity_us",
          forecast_target: null, horizon: null, probability: 0.7, probability_range_low: null,
          probability_range_high: null, resolution_source: null, review_date: "2026-06-20",
          status: "closed", outcome: true, observed_value: 175.5, resolved_at: "2026-06-21T00:00:00Z",
          brier_score: 0.09, created_at: "2026-06-10T00:00:00Z",
        }],
      };
    } else if (u.includes("/artifacts")) {
      body = { success: true, count: 0, filters: {}, artifacts: [] };
    } else if (u.includes("/session-context")) {
      body = { success: true, count: 0, filters: {}, entries: [] };
    } else if (u.includes("next-actions")) {
      body = { market: "all", symbol: null, count: 0, scan_limit: 200, items: [] };
    } else if (u.includes("retrospectives")) {
      body = {
        market: "all", trigger_type: null, root_cause_class: null, symbol: null, count: 1, total: 1, as_of: "2026-07-03T00:00:00Z",
        items: [{
          id: 1, correlation_id: "toss_live:uuid", symbol: retroSymbol, market: "us",
          instrument_type: "equity_us", side: "sell", trigger_type: "fill", root_cause_class: null,
          outcome: "win", realized_pnl: 100, realized_pnl_currency: "USD", pnl_pct: 1.2,
          result_summary: null, lesson: `${retroSymbol} 매도 회고`, next_strategy: null,
          intended_vs_happened: null, next_actions: null, guardrail_fired: null,
          policy_version: null, created_at: "2026-07-01T00:00:00Z",
        }],
      };
    }
    return { ok: true, status: 200, json: async () => body };
  });
}

test("crosslinks closed forecast <-> retrospective by symbol key, not correlation_id (ROB-682)", async () => {
  vi.stubGlobal("fetch", buildCrosslinkFetchMock("AAPL") as unknown as typeof fetch);

  render(wrap(<DesktopInsightsPage />));

  // correlation_ids ("aapl-thesis" vs "toss_live:uuid") are disjoint — under
  // ROB-678's exact-correlation_id scheme this crosslink would be dead.
  const forecastLink = await screen.findByRole("link", { name: /회고/ });
  expect(forecastLink).toHaveAttribute("href", "#retro-us-AAPL");
  expect(document.getElementById("forecast-us-AAPL")).not.toBeNull();

  const retroLink = await screen.findByRole("link", { name: "예측↑" });
  expect(retroLink).toHaveAttribute("href", "#forecast-us-AAPL");
  expect(document.getElementById("retro-us-AAPL")).not.toBeNull();
});

test("does not crosslink when symbols differ across axes (ROB-682)", async () => {
  vi.stubGlobal("fetch", buildCrosslinkFetchMock("TSLA") as unknown as typeof fetch);

  render(wrap(<DesktopInsightsPage />));

  await screen.findByText("AAPL");
  await screen.findByText(/TSLA 매도 회고/);
  expect(screen.queryByRole("link", { name: /회고/ })).toBeNull();
  expect(screen.queryByRole("link", { name: "예측↑" })).toBeNull();
});
