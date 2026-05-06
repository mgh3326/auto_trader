import { render, screen } from "@testing-library/react";
import { GroupedRow, RawRow } from "../components/HoldingRow";
import type { GroupedHolding, Holding } from "../types/invest";
import { expect, test } from "vitest";

const grouped: GroupedHolding = {
  groupId: "KR:equity:KRW:005930",
  symbol: "005930",
  market: "KR",
  assetType: "equity",
  assetCategory: "kr_stock",
  displayName: "삼성전자",
  currency: "KRW",
  totalQuantity: 30,
  averageCost: 70000,
  costBasis: 2100000,
  valueNative: 2148000,
  valueKrw: 2148000,
  pnlKrw: 48000,
  pnlRate: 0.0228,
  priceState: "live",
  includedSources: ["kis", "toss_manual"],
  sourceBreakdown: [],
};

test("GroupedRow renders symbol and multiple source badge", () => {
  render(<GroupedRow row={grouped} />);
  expect(screen.getByText(/005930/)).toBeInTheDocument();
  expect(screen.getByText(/삼성전자/)).toBeInTheDocument();
  expect(screen.getByText(/KIS · Toss 수동/)).toBeInTheDocument();
});

test("GroupedRow handles single source badge naming", () => {
  render(<GroupedRow row={{ ...grouped, includedSources: ["kis"] }} />);
  expect(screen.getByText("KIS")).toBeInTheDocument();
});

test("RawRow renders single source badge", () => {
  const raw: Holding = {
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
    valueKrw: 1244000,
    pnlKrw: -16000,
    pnlRate: -0.017,
    priceState: "live",
  };
  render(<RawRow row={raw} />);
  expect(screen.getByText(/TSLA/)).toBeInTheDocument();
  expect(screen.getByText("Toss 수동")).toBeInTheDocument();
});
