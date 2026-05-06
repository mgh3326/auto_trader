import { render, screen, fireEvent } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { HomePage } from "../pages/HomePage";
import type { InvestHomeResponse } from "../types/invest";
import { expect, test } from "vitest";

const data: InvestHomeResponse = {
  homeSummary: {
    includedSources: ["kis", "toss_manual"],
    excludedSources: ["kis_mock"],
    totalValueKrw: 12_000_000,
    costBasisKrw: 9_000_000,
    pnlKrw: 3_000_000,
    pnlRate: 1 / 3,
  },
  accounts: [
    {
      accountId: "a1",
      displayName: "Toss 수동",
      source: "toss_manual",
      accountKind: "manual",
      includedInHome: true,
      valueKrw: 1_244_000,
      costBasisKrw: 1_260_000,
      pnlKrw: -16_000,
      pnlRate: -0.012,
      cashBalances: {},
      buyingPower: {},
    },
  ],
  holdings: [
    {
      holdingId: "h1",
      accountId: "a1",
      source: "toss_manual",
      accountKind: "manual",
      symbol: "TSLA",
      market: "US",
      assetType: "equity",
      assetCategory: "us_stock",
      displayName: "Tesla",
      quantity: 4,
      averageCost: 234,
      costBasis: 936,
      currency: "USD",
      valueNative: 924,
      valueKrw: 1_244_000,
      pnlKrw: -16_000,
      pnlRate: -0.012,
      priceState: "live",
    },
  ],
  groupedHoldings: [
    {
      groupId: "US:equity:USD:TSLA",
      symbol: "TSLA",
      market: "US",
      assetType: "equity",
      assetCategory: "us_stock",
      displayName: "Tesla",
      currency: "USD",
      totalQuantity: 4,
      averageCost: 234,
      costBasis: 936,
      valueNative: 924,
      valueKrw: 1_244_000,
      pnlKrw: -16_000,
      pnlRate: -0.012,
      priceState: "live",
      includedSources: ["toss_manual"],
      sourceBreakdown: [],
    },
  ],
  meta: {
    warnings: [{ source: "upbit", message: "cache only" }],
    hiddenCounts: { upbitInactive: 0, upbitDust: 0 },
    hiddenHoldings: [],
  },
};

test("renders meta.warnings as a single line", () => {
  render(
    <MemoryRouter basename="/invest/app" initialEntries={["/invest/app/"]}>
      <HomePage state={{ status: "ready", data }} reload={() => {}} />
    </MemoryRouter>
  );
  expect(screen.getByText(/cache only/)).toBeInTheDocument();
});

test("account selector toggles between groupedHoldings and raw holdings", () => {
  render(
    <MemoryRouter basename="/invest/app" initialEntries={["/invest/app/"]}>
      <HomePage state={{ status: "ready", data }} reload={() => {}} />
    </MemoryRouter>
  );
  expect(screen.getByTestId("grouped-row")).toBeInTheDocument();
  // "Toss 수동" is now in AccountSelector which uses buttons
  fireEvent.click(screen.getByRole("button", { name: "Toss 수동" }));
  expect(screen.queryByTestId("grouped-row")).toBeNull();
  expect(screen.getByTestId("raw-row")).toBeInTheDocument();
});

test("renders empty account state with portfolio deeplink", () => {
  render(
    <MemoryRouter basename="/invest/app" initialEntries={["/invest/app/"]}>
      <HomePage
        state={{
          status: "ready",
          data: {
            ...data,
            accounts: [],
            holdings: [],
            groupedHoldings: [],
            meta: {
              warnings: [],
              hiddenCounts: { upbitInactive: 0, upbitDust: 0 },
              hiddenHoldings: [],
            },
          },
        }}
        reload={() => {}}
      />
    </MemoryRouter>
  );

  expect(screen.getByText("연결된 계좌가 없습니다")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "포트폴리오로 이동" })).toHaveAttribute(
    "href",
    "/portfolio/",
  );
});
