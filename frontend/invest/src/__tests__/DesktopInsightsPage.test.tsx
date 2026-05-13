import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

import { DesktopInsightsPage } from "../pages/desktop/DesktopInsightsPage";
import { useCommonPreferredDisparity } from "../hooks/useCommonPreferredDisparity";
import { useMarketParity } from "../hooks/useMarketParity";

vi.mock("../hooks/useMarketParity", () => ({ useMarketParity: vi.fn() }));
vi.mock("../hooks/useCommonPreferredDisparity", () => ({ useCommonPreferredDisparity: vi.fn() }));

const marketParityReady = {
  state: {
    status: "ready" as const,
    data: {
      asOf: "2026-05-14T00:00:00Z",
      market: "kr" as const,
      state: "fresh" as const,
      emptyReason: null,
      warnings: [],
      notes: ["read-only"],
      cards: [
        {
          id: "kospi-etf",
          title: "KOSPI ETF parity",
          type: "index_implied_parity" as const,
          baseSymbol: "KOSPI",
          baseName: "KOSPI",
          proxySymbol: "069500",
          syntheticSymbol: null,
          formula: "ETF/NAV",
          premiumPct: 1.23,
          tone: "premium" as const,
          dataState: "fresh" as const,
          emptyReason: null,
          source: {
            source: "read-only fixture",
            sourceOfTruth: "market parity service",
            asOf: "2026-05-14T00:00:00Z",
            freshnessSec: 60,
            stale: false,
            warnings: [],
          },
        },
      ],
    },
  },
  reload: vi.fn(),
};

const disparityReady = {
  status: "ready" as const,
  data: {
    asOf: "2026-05-14T00:00:00Z",
    market: "kr" as const,
    state: "fresh" as const,
    emptyReason: null,
    warnings: [],
    notes: ["read-only"],
    cards: [
      {
        id: "005930-005935",
        commonSymbol: "005930",
        commonName: "삼성전자",
        preferredSymbol: "005935",
        preferredName: "삼성전자우",
        commonPrice: 80000,
        preferredPrice: 65000,
        disparityPct: -18.75,
        zScore: -1.2,
        tone: "discount" as const,
        dataState: "fresh" as const,
        emptyReason: null,
        primaryWindow: "60d" as const,
        windows: [{ period: "60d" as const, sampleCount: 60, meanDisparityPct: -15, zScore: -1.2, dataState: "fresh" as const }],
        formula: "preferred/common - 1",
        source: {
          source: "read-only fixture",
          sourceOfTruth: "common preferred disparity service",
          asOf: "2026-05-14T00:00:00Z",
          stale: false,
          freshnessSec: 60,
          warnings: [],
        },
        caution: "매수·매도 추천이 아닙니다.",
        warnings: [],
      },
    ],
  },
};

function wrap(ui: React.ReactElement) {
  return <MemoryRouter basename="/invest" initialEntries={["/invest/insights"]}>{ui}</MemoryRouter>;
}

beforeEach(() => {
  vi.mocked(useMarketParity).mockReturnValue(marketParityReady);
  vi.mocked(useCommonPreferredDisparity).mockReturnValue(disparityReady);
});

test("renders the dedicated read-only insights scaffold", () => {
  render(wrap(<DesktopInsightsPage />));

  expect(screen.getByRole("heading", { name: "인사이트" })).toBeInTheDocument();
  expect(screen.getByText(/ROB-253 decision/)).toBeInTheDocument();
  expect(screen.getByText(/주문·매매·watch mutation API를 호출하지 않습니다/)).toBeInTheDocument();
  expect(screen.getByText("KOSPI ETF parity")).toBeInTheDocument();
  expect(screen.getByText("삼성전자 / 삼성전자우")).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "시장 대시보드" })).toHaveAttribute("href", "/invest/market");
});

test("renders loading and error states for independent insight widgets", () => {
  vi.mocked(useMarketParity).mockReturnValue({ state: { status: "error", message: "boom" }, reload: vi.fn() });
  vi.mocked(useCommonPreferredDisparity).mockReturnValue({ status: "loading" });

  render(wrap(<DesktopInsightsPage />));

  expect(screen.getByText(/괴리 참고 카드를 일시적으로 불러오지 못했습니다/)).toBeInTheDocument();
  expect(screen.getByText(/보통주\/우선주 괴리 데이터를 불러오는 중/)).toBeInTheDocument();
});

test("renders an empty market parity state without hiding the page", () => {
  vi.mocked(useMarketParity).mockReturnValue({
    state: {
      status: "ready",
      data: { ...marketParityReady.state.data, state: "missing", emptyReason: "승인된 패리티 카드가 없습니다.", cards: [] },
    },
    reload: vi.fn(),
  });

  render(wrap(<DesktopInsightsPage />));

  expect(screen.getByText("승인된 패리티 카드가 없습니다.")).toBeInTheDocument();
  expect(screen.getByText("삼성전자 / 삼성전자우")).toBeInTheDocument();
});
