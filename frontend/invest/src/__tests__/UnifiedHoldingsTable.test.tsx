import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";
import { UnifiedHoldingsTable } from "../components/my/UnifiedHoldingsTable";
import type { Account, GroupedHolding } from "../types/invest";

const accounts: Account[] = [
  {
    accountId: "kis-main",
    displayName: "KIS 종합",
    source: "kis",
    accountKind: "live",
    includedInHome: true,
    valueKrw: 2148000,
    costBasisKrw: 2100000,
    pnlKrw: 48000,
    pnlRate: 0.0228,
    cashBalances: {},
    buyingPower: {},
  },
  {
    accountId: "toss-benchmark",
    displayName: "Toss 수동 벤치마크",
    source: "toss_manual",
    accountKind: "manual",
    includedInHome: true,
    valueKrw: 716000,
    costBasisKrw: 700000,
    pnlKrw: 16000,
    pnlRate: 0.0228,
    cashBalances: {},
    buyingPower: {},
  },
];

const holdings: GroupedHolding[] = [
  {
    groupId: "KR:equity:KRW:005930",
    symbol: "005930",
    market: "KR",
    assetType: "equity",
    assetCategory: "kr_stock",
    displayName: "삼성전자",
    currency: "KRW",
    totalQuantity: 40,
    tradeableQuantity: 30,
    sellableQuantity: 25,
    pendingSellQuantity: 5,
    referenceQuantity: 10,
    averageCost: 70000,
    costBasis: 2800000,
    valueNative: 2864000,
    valueKrw: 2864000,
    pnlKrw: 64000,
    pnlRate: 0.0228,
    priceState: "stale",
    includedSources: ["kis", "toss_manual"],
    sourceBreakdown: [
      {
        holdingId: "h-kis",
        accountId: "kis-main",
        source: "kis",
        quantity: 30,
        accountKind: "live",
        sourceOfTruth: true,
        isTradeable: true,
        manualOnly: false,
        sellableQuantity: 25,
        pendingSellQuantity: 5,
        referenceQuantity: 0,
        averageCost: 70000,
        valueNative: 2148000,
        valueKrw: 2148000,
        pnlKrw: 48000,
        pnlRate: 0.0228,
      },
      {
        holdingId: "h-toss",
        accountId: "toss-benchmark",
        source: "toss_manual",
        quantity: 10,
        accountKind: "manual",
        sourceOfTruth: false,
        isTradeable: false,
        manualOnly: true,
        sellableQuantity: 0,
        pendingSellQuantity: 0,
        referenceQuantity: 10,
        averageCost: 70000,
        valueNative: 716000,
        valueKrw: 716000,
        pnlKrw: 16000,
        pnlRate: 0.0228,
      },
    ],
  },
];

test("UnifiedHoldingsTable renders source/account breakdown and stock detail link", () => {
  render(
    <MemoryRouter>
      <UnifiedHoldingsTable holdings={holdings} accounts={accounts} />
    </MemoryRouter>,
  );

  const row = screen.getByTestId("unified-holding-row");
  expect(row).toHaveAttribute("href", "/stocks/kr/005930");
  expect(within(row).getByText("삼성전자")).toBeInTheDocument();
  expect(within(row).getByText("시세 지연")).toBeInTheDocument();
  expect(within(row).getAllByText("KIS 종합").length).toBeGreaterThan(0);
  expect(within(row).getAllByText("Toss 수동 벤치마크").length).toBeGreaterThan(0);
  expect(within(row).getByText(/매매가능 30주/)).toBeInTheDocument();
  expect(within(row).getAllByText(/매도가능 25주/).length).toBeGreaterThan(0);
  expect(within(row).getByText(/주문대기 5주/)).toBeInTheDocument();
  expect(within(row).getAllByText(/참고전용 10주/).length).toBeGreaterThan(0);
  expect(screen.getAllByTestId("unified-holding-source-breakdown")).toHaveLength(2);
});

test("UnifiedHoldingsTable renders explicit empty state", () => {
  render(
    <MemoryRouter>
      <UnifiedHoldingsTable holdings={[]} accounts={accounts} />
    </MemoryRouter>,
  );

  expect(screen.getByTestId("unified-holdings-empty")).toHaveTextContent("표시할 보유 종목이 없습니다");
});
