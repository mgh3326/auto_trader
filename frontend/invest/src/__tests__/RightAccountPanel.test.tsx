import { render, screen } from "@testing-library/react";
import { expect, test } from "vitest";
import { RightAccountPanel } from "../desktop/RightAccountPanel";
import type { AccountPanelResponse } from "../types/invest";

const baseResp: AccountPanelResponse = {
  homeSummary: {
    includedSources: ["kis"], excludedSources: [],
    totalValueKrw: 1_000_000, pnlKrw: 50_000, pnlRate: 0.05,
  },
  accounts: [{
    accountId: "k1", displayName: "KIS Live", source: "kis",
    accountKind: "live", includedInHome: true, valueKrw: 1_000_000,
    cashBalances: { krw: 50_000 }, buyingPower: { krw: 50_000 },
  }],
  groupedHoldings: [],
  watchSymbols: [],
  sourceVisuals: [
    { source: "kis", tone: "navy", badge: "Live", displayName: "KIS" },
    { source: "upbit", tone: "purple", badge: "Crypto", displayName: "Upbit" },
  ],
  meta: { warnings: [], watchlistAvailable: true },
};

test("renders skeleton when loading", () => {
  render(<RightAccountPanel loading />);
  expect(screen.getByTestId("right-panel-skeleton")).toBeInTheDocument();
});

test("renders accounts with source-based badge", () => {
  render(<RightAccountPanel data={baseResp} />);
  expect(screen.getByTestId("right-panel")).toBeInTheDocument();
  const card = screen.getByTestId("right-panel-account");
  expect(card.dataset.source).toBe("kis");
  expect(card.textContent).toContain("Live");
});

test("keeps KIS mock visibly distinct from live account", () => {
  const data: AccountPanelResponse = {
    ...baseResp,
    accounts: [
      ...baseResp.accounts,
      {
        accountId: "km1",
        displayName: "KIS official mock",
        source: "kis_mock",
        accountKind: "paper",
        includedInHome: false,
        valueKrw: 0,
        cashBalances: { krw: 1_000_000, usd: 10 },
        buyingPower: { krw: 1_000_000, usd: 10 },
      },
    ],
    sourceVisuals: [
      ...baseResp.sourceVisuals,
      { source: "kis_mock", tone: "dashed", badge: "Mock", displayName: "KIS mock" },
    ],
  };

  render(<RightAccountPanel data={data} />);

  const cards = screen.getAllByTestId("right-panel-account");
  expect(cards.map((card) => card.dataset.source)).toEqual(["kis", "kis_mock"]);
  expect(screen.getByText("KIS official mock · KIS 모의")).toBeInTheDocument();
  expect(screen.getByText("Mock")).toBeInTheDocument();
  expect(screen.getByText("$10.00")).toBeInTheDocument();
});

test("watchlist empty state", () => {
  render(<RightAccountPanel data={baseResp} />);
  expect(screen.getByTestId("watchlist-empty")).toBeInTheDocument();
});
