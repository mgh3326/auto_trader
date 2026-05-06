import { render, screen, fireEvent } from "@testing-library/react";
import { HomePage } from "../pages/HomePage";
import type { InvestHomeResponse } from "../types/invest";

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
      displayName: "Tesla",
      quantity: 4,
      averageCost: 234,
      costBasis: 936,
      currency: "USD",
      valueNative: 924,
      valueKrw: 1_244_000,
      pnlKrw: -16_000,
      pnlRate: -0.012,
    },
  ],
  groupedHoldings: [
    {
      groupId: "US:equity:USD:TSLA",
      symbol: "TSLA",
      market: "US",
      assetType: "equity",
      displayName: "Tesla",
      currency: "USD",
      totalQuantity: 4,
      averageCost: 234,
      costBasis: 936,
      valueNative: 924,
      valueKrw: 1_244_000,
      pnlKrw: -16_000,
      pnlRate: -0.012,
      includedSources: ["toss_manual"],
      sourceBreakdown: [],
    },
  ],
  meta: { warnings: [{ source: "upbit", message: "cache only" }] },
};

test("renders meta.warnings as a single line", () => {
  render(<HomePage state={{ status: "ready", data }} reload={() => {}} />);
  expect(screen.getByText(/cache only/)).toBeInTheDocument();
});

test("activeSource toggles between groupedHoldings and raw holdings", () => {
  render(<HomePage state={{ status: "ready", data }} reload={() => {}} />);
  expect(screen.getByTestId("grouped-row")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Toss 수동" }));
  expect(screen.queryByTestId("grouped-row")).toBeNull();
  expect(screen.getByTestId("raw-row")).toBeInTheDocument();
});

test("renders empty account state with portfolio deeplink", () => {
  render(
    <HomePage
      state={{
        status: "ready",
        data: {
          ...data,
          accounts: [],
          holdings: [],
          groupedHoldings: [],
          meta: { warnings: [] },
        },
      }}
      reload={() => {}}
    />,
  );

  expect(screen.getByText("연결된 계좌가 없습니다")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "포트폴리오로 이동" })).toHaveAttribute(
    "href",
    "/portfolio/",
  );
});
