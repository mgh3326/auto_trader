import { render, screen } from "@testing-library/react";
import { GroupedRow, RawRow } from "../components/HoldingRow";
import type { GroupedHolding, Holding } from "../types/invest";

const grouped: GroupedHolding = {
  groupId: "KR:equity:KRW:005930",
  symbol: "005930",
  market: "KR",
  assetType: "equity",
  displayName: "삼성전자",
  currency: "KRW",
  totalQuantity: 50,
  averageCost: 70_400,
  costBasis: 3_520_000,
  valueNative: 3_580_000,
  valueKrw: 3_580_000,
  pnlKrw: 60_000,
  pnlRate: 60_000 / 3_520_000,
  includedSources: ["kis", "toss_manual"],
  sourceBreakdown: [],
};

test("GroupedRow shows includedSources chip 'KIS · Toss 수동'", () => {
  render(<GroupedRow row={grouped} />);
  expect(screen.getByText(/KIS/)).toBeInTheDocument();
  expect(screen.getByText(/Toss/)).toBeInTheDocument();
});

test("GroupedRow renders '-' when averageCost is null", () => {
  render(
    <GroupedRow
      row={{
        ...grouped,
        averageCost: null,
        pnlRate: null,
        costBasis: null,
        pnlKrw: null,
      }}
    />
  );
  expect(screen.getAllByText("-").length).toBeGreaterThan(0);
});

test("RawRow renders single source pill", () => {
  const raw: Holding = {
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
    pnlRate: -16_000 / 1_260_000,
  };
  render(<RawRow row={raw} />);
  expect(screen.getByText(/Toss/)).toBeInTheDocument();
});
