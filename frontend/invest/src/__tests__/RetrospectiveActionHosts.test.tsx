import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { MobileInsightsPage } from "../pages/mobile/MobileInsightsPage";
import { DesktopPortfolioPage } from "../pages/desktop/DesktopPortfolioPage";
import { MobilePortfolioPage } from "../pages/mobile/MobilePortfolioPage";
import { useCommonPreferredDisparity } from "../hooks/useCommonPreferredDisparity";
import { useMarketParity } from "../hooks/useMarketParity";
import { useInvestHome } from "../hooks/useInvestHome";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type { InvestHomeResponse } from "../types/invest";

vi.mock("../hooks/useMarketParity", () => ({ useMarketParity: vi.fn() }));
vi.mock("../hooks/useCommonPreferredDisparity", () => ({
  useCommonPreferredDisparity: vi.fn(),
}));
vi.mock("../hooks/useInvestHome", () => ({ useInvestHome: vi.fn() }));

const emptyActionsBody = {
  total: 0, count: 0, limit: 10, offset: 0,
  as_of: "2026-07-15T00:00:00Z", items: [],
};
const emptyRetroBody = {
  market: "all", trigger_type: null, root_cause_class: null, symbol: null,
  outcome_filter: null, q: null, kst_date_from: null, kst_date_to: null,
  count: 0, total: 0, items: [], as_of: "2026-07-15T00:00:00Z",
};

function stubFetchForPanels() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const u = String(url);
      let body: unknown = {};
      if (u.includes("/scoreboard")) {
        body = { group_by: "strategy", market: "all", kst_date_from: null, kst_date_to: null, count: 0, groups: [], as_of: "2026-07-15T00:00:00Z", totals: { sample_size: 0, wins: 0, misses: 0, decided: 0, win_rate_pct: null, realized_pnl_sum: {}, fx_pnl_krw_sum: 0, total_pnl_krw_sum: 0, excluded_no_fill_evidence: 0 } };
      } else if (u.includes("/calibration")) {
        body = { group_by: "created_by", created_by: null, symbol: null, instrument_type: null, days: null, count: 0, groups: [], as_of: "2026-07-15T00:00:00Z" };
      } else if (u.includes("/forecasts/open") || u.includes("/forecasts/closed")) {
        body = { kind: "open", symbol: null, created_by: null, instrument_type: null, count: 0, items: [], as_of: "2026-07-15T00:00:00Z" };
      } else if (u.includes("/artifacts")) {
        body = { success: true, count: 0, filters: {}, artifacts: [] };
      } else if (u.includes("/session-context")) {
        body = { success: true, count: 0, filters: {}, entries: [] };
      } else if (u.includes("/actions")) {
        body = emptyActionsBody;
      } else if (u.includes("retrospectives")) {
        body = emptyRetroBody;
      }
      return { ok: true, status: 200, json: async () => body };
    }) as unknown as typeof fetch,
  );
}

const minimalHome: InvestHomeResponse = {
  homeSummary: {
    includedSources: [], excludedSources: [], totalValueKrw: 0,
    costBasisKrw: null, pnlKrw: null, pnlRate: null,
  },
  accounts: [],
  holdings: [],
  groupedHoldings: [],
  meta: { warnings: [], hiddenCounts: { upbitInactive: 0, upbitDust: 0 }, hiddenHoldings: [] },
};

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  stubFetchForPanels();
  vi.mocked(useMarketParity).mockReturnValue({
    state: { status: "ready", data: { asOf: "x", market: "kr", state: "fresh", emptyReason: null, warnings: [], notes: [], cards: [] } },
    reload: vi.fn(),
  });
  vi.mocked(useCommonPreferredDisparity).mockReturnValue({ status: "ready", data: { asOf: "x", market: "kr", state: "fresh", emptyReason: null, warnings: [], notes: [], cards: [] } });
  vi.mocked(useInvestHome).mockReturnValue({ state: { status: "ready", data: minimalHome }, reload: vi.fn() });
});

afterEach(() => vi.unstubAllGlobals());

test("ROB-885: MobileInsightsPage (compact) mounts the canonical action section", async () => {
  render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/insights"]}>
        <MobileInsightsPage />
      </MemoryRouter>
    </AccountPanelProvider>,
  );

  await waitFor(() => expect(screen.getByTestId("retrospectives-panel")).toBeInTheDocument());
  expect(screen.getByTestId("retro-actions")).toBeInTheDocument();
});

test("ROB-885: DesktopPortfolioPage 회고 tab (normal) mounts the canonical action section", async () => {
  render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=retrospectives"]}>
        <DesktopPortfolioPage />
      </MemoryRouter>
    </AccountPanelProvider>,
  );

  await waitFor(() => expect(screen.getByTestId("retrospectives-panel")).toBeInTheDocument());
  expect(screen.getByTestId("retro-actions")).toBeInTheDocument();
});

test("ROB-885: MobilePortfolioPage 회고 tab (compact) mounts the canonical action section", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=retrospectives"]}>
      <MobilePortfolioPage />
    </MemoryRouter>,
  );

  await waitFor(() => expect(screen.getByTestId("retrospectives-panel")).toBeInTheDocument());
  expect(screen.getByTestId("retro-actions")).toBeInTheDocument();
});
