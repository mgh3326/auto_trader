import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, test, expect } from "vitest";
import { MemoryRouter } from "react-router-dom";
import { RightRemotePanel } from "../desktop/RightRemotePanel";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import * as panelApi from "../api/accountPanel";
import * as signalsApi from "../api/signals";
import type { AccountPanelResponse } from "../types/invest";

const PANEL_RESP: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis"],
    excludedSources: [],
    totalValueKrw: 5_000_000,
    pnlKrw: 250_000,
    pnlRate: 0.05,
  },
  accounts: [
    {
      accountId: "k1",
      displayName: "KIS Live",
      source: "kis",
      accountKind: "live",
      includedInHome: true,
      valueKrw: 5_000_000,
      cashBalances: { krw: 100_000 },
      buyingPower: { krw: 100_000 },
    },
  ],
  groupedHoldings: [
    {
      groupId: "g1",
      symbol: "005930",
      market: "KR",
      assetType: "equity",
      assetCategory: "kr_stock",
      displayName: "삼성전자",
      currency: "KRW",
      totalQuantity: 10,
      valueKrw: 800_000,
      pnlKrw: 40_000,
      pnlRate: 0.05,
      priceState: "live",
      includedSources: ["kis"],
      sourceBreakdown: [],
    },
  ],
  watchSymbols: [
    { symbol: "AAPL", market: "us", displayName: "Apple Inc." },
  ],
  sourceVisuals: [{ source: "kis", tone: "navy", badge: "Live", displayName: "KIS" }],
  meta: { warnings: [], watchlistAvailable: true },
};

function renderPanel() {
  return render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
        <RightRemotePanel />
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue(PANEL_RESP);
  vi.spyOn(signalsApi, "fetchSignals").mockResolvedValue({
    tab: "kr",
    asOf: new Date().toISOString(),
    items: [],
    meta: { warnings: [] },
  });
  localStorage.clear();
});

test("renders the tabbed right remote panel", async () => {
  renderPanel();
  expect(screen.getByTestId("right-remote-panel")).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "내 투자" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "관심" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "최근 본" })).toBeInTheDocument();
  expect(screen.getByRole("tab", { name: "실시간" })).toBeInTheDocument();
});

test("portfolio tab shows holdings after data loads", async () => {
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  expect(screen.getByText("삼성전자")).toBeInTheDocument();
  expect(screen.getByText("₩800,000")).toBeInTheDocument();
});

test("watchlist tab shows watch symbols", async () => {
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  await userEvent.click(screen.getByRole("tab", { name: "관심" }));
  expect(screen.getByTestId("watchlist-panel")).toBeInTheDocument();
  expect(screen.getByText("Apple Inc.")).toBeInTheDocument();
});

test("recent tab shows empty state initially", async () => {
  renderPanel();
  await userEvent.click(screen.getByRole("tab", { name: "최근 본" }));
  expect(screen.getByTestId("recent-panel-empty")).toBeInTheDocument();
});

test("portfolio tab shows empty holdings gracefully", async () => {
  vi.spyOn(panelApi, "fetchAccountPanel").mockResolvedValue({
    ...PANEL_RESP,
    groupedHoldings: [],
  });
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("holdings-empty")).toBeInTheDocument());
});

test("does not render order CTA buttons", async () => {
  renderPanel();
  await waitFor(() => expect(screen.getByTestId("portfolio-panel")).toBeInTheDocument());
  expect(screen.queryByRole("button", { name: "매수" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "매도" })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /주문/ })).not.toBeInTheDocument();
});
