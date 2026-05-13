import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { expect, test } from "vitest";

import { MarketStrip, marketDashboardToStripItems } from "../components/home/MarketStrip";
import type { MarketDashboardResponse } from "../types/marketDashboard";

const MARKET_PAYLOAD: MarketDashboardResponse = {
  asOf: "2026-05-13T03:00:00Z",
  state: "partial",
  warnings: [],
  notes: [],
  sections: [
    {
      id: "kr_market",
      title: "국내 시장",
      subtitle: "국내 지수",
      reference: "naver",
      state: "fresh",
      sourceOfTruth: "get_market_index",
      updatedAt: "2026-05-13T03:00:00Z",
      staleAfterMinutes: 20,
      warnings: [],
      notes: [],
      metrics: [
        { label: "코스피", value: "2,900.00", change: 3, changePct: 0.1, tone: "up", source: "naver", stale: false },
      ],
    },
    {
      id: "fx_macro",
      title: "FX·매크로",
      subtitle: "환율/매크로",
      reference: "fx_dashboard",
      state: "partial",
      sourceOfTruth: "invest_fx_dashboard",
      updatedAt: "2026-05-13T03:00:00Z",
      staleAfterMinutes: 20,
      warnings: ["partial"],
      notes: ["read-only"],
      metrics: [
        { label: "USD/KRW", value: "1,450.25", change: 2.1, changePct: 0.15, tone: "up", source: "naver", stale: false },
      ],
    },
  ],
};

test("market strip includes concise FX macro entry linking to detail page", () => {
  const items = marketDashboardToStripItems(MARKET_PAYLOAD);
  expect(items.some((item) => item.name === "FX·매크로 USD/KRW" && item.href === "/market/fx")).toBe(true);

  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/"]}>
      <MarketStrip items={items} />
    </MemoryRouter>,
  );

  expect(screen.getByRole("link", { name: /FX·매크로 USD\/KRW/ })).toHaveAttribute("href", "/invest/market/fx");
  expect(screen.getByText("상세 보기")).toBeInTheDocument();
});
