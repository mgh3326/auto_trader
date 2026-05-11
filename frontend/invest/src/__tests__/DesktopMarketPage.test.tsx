import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { DesktopMarketPage } from "../pages/desktop/DesktopMarketPage";
import * as marketApi from "../api/marketDashboard";

const MARKET_PAYLOAD = {
  asOf: "2026-05-11T05:00:00Z",
  state: "partial" as const,
  warnings: ["kimchi_premium: timeout"],
  notes: ["No broker/order/watch-order mutations or scheduled collectors are invoked."],
  sections: [
    {
      id: "kr_market" as const,
      title: "국내 시장",
      subtitle: "코스피·코스닥 현재가와 등락률",
      reference: "naver",
      state: "fresh" as const,
      sourceOfTruth: "get_market_index(KOSPI/KOSDAQ)",
      updatedAt: "2026-05-11T05:00:00Z",
      staleAfterMinutes: 20,
      metrics: [
        {
          label: "코스피",
          value: "2,875.25",
          change: 12.3,
          changePct: 0.43,
          tone: "up" as const,
          unit: null,
          source: "naver",
          symbol: "KOSPI",
          href: null,
          stale: false,
          warning: null,
        },
      ],
      warnings: [],
      notes: ["Naver 증권 시장 홈의 국내 지수 영역"],
    },
    {
      id: "crypto_market" as const,
      title: "가상자산 시장",
      subtitle: "BTC 기준 김치 프리미엄",
      reference: "naver",
      state: "partial" as const,
      sourceOfTruth: "get_kimchi_premium(BTC)",
      updatedAt: null,
      staleAfterMinutes: 30,
      metrics: [
        {
          label: "김치 프리미엄",
          value: null,
          change: null,
          changePct: null,
          tone: "unknown" as const,
          unit: "%",
          source: "upbit/binance",
          symbol: "BTC",
          href: null,
          stale: true,
          warning: "kimchi_premium: timeout",
        },
      ],
      warnings: ["kimchi_premium: timeout"],
      notes: ["투자 조언이 아닌 시장 상태 참고용"],
    },
  ],
};

function wrap(ui: React.ReactElement) {
  return <MemoryRouter basename="/invest" initialEntries={["/invest/market"]}>{ui}</MemoryRouter>;
}

beforeEach(() => {
  vi.spyOn(marketApi, "fetchMarketDashboard").mockResolvedValue(MARKET_PAYLOAD);
});

test("renders market dashboard sections and read-only copy", async () => {
  render(wrap(<DesktopMarketPage />));

  await waitFor(() => expect(screen.getByText("코스피")).toBeInTheDocument());
  expect(screen.getByRole("heading", { name: "시장" })).toBeInTheDocument();
  expect(screen.getByText("2,875.25")).toBeInTheDocument();
  expect(screen.getByText("가상자산 시장")).toBeInTheDocument();
  expect(screen.getAllByText(/kimchi_premium: timeout/).length).toBeGreaterThan(0);
  expect(screen.getByText(/주문·매매 API를 호출하지 않습니다/)).toBeInTheDocument();
});

test("shows a friendly message when market dashboard fails", async () => {
  vi.spyOn(marketApi, "fetchMarketDashboard").mockRejectedValue(new Error("/invest/api/market 500"));

  render(wrap(<DesktopMarketPage />));

  await waitFor(() => expect(screen.getByText(/시장 데이터를 일시적으로 불러오지 못했습니다/)).toBeInTheDocument());
  expect(screen.queryByText(/500/)).not.toBeInTheDocument();
});
