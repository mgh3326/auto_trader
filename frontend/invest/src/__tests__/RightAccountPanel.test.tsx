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

test("watchlist empty state", () => {
  render(<RightAccountPanel data={baseResp} />);
  expect(screen.getByTestId("watchlist-empty")).toBeInTheDocument();
});
