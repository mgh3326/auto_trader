import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { ScreenerResultsTable } from "../desktop/screener/ScreenerResultsTable";
import type { ScreenerResultRow } from "../types/screener";

const BASE_ROW: ScreenerResultRow = {
  rank: 1,
  symbol: "005930",
  market: "kr",
  name: "삼성전자",
  logoUrl: null,
  isWatched: false,
  priceLabel: "80,000원",
  changePctLabel: "+1.23%",
  changeAmountLabel: "+970원",
  changeDirection: "up",
  category: "반도체",
  marketCapLabel: "478조원",
  volumeLabel: "12,345,678",
  analystLabel: "구매",
  metricValueLabel: "+8.00%",
  investorFlowChip: null,
  warnings: [],
};

describe("ScreenerResultsTable market_cap source badge (ROB-426 PR3)", () => {
  it("renders the '참고' badge when marketCapSource is 'fallback'", () => {
    render(
      <ScreenerResultsTable
        rows={[{ ...BASE_ROW, marketCapSource: "fallback" }]}
        metricLabel="주가등락률"
      />,
    );
    expect(screen.getByText("참고")).toBeInTheDocument();
  });

  it("does not render the badge when marketCapSource is 'primary'", () => {
    render(
      <ScreenerResultsTable
        rows={[{ ...BASE_ROW, marketCapSource: "primary" }]}
        metricLabel="주가등락률"
      />,
    );
    expect(screen.queryByText("참고")).not.toBeInTheDocument();
  });

  it("does not render the badge when marketCapSource is absent", () => {
    render(<ScreenerResultsTable rows={[BASE_ROW]} metricLabel="주가등락률" />);
    expect(screen.queryByText("참고")).not.toBeInTheDocument();
  });
});
