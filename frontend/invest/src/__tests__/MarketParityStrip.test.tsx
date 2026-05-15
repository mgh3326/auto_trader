import { fireEvent, render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import { MarketParityStrip } from "../components/home/MarketParityStrip";
import type { MarketParityResponse } from "../types/marketParity";

const PAYLOAD: MarketParityResponse = {
  market: "kr",
  state: "partial",
  asOf: "2026-05-14T00:00:00Z",
  warnings: ["stablecoin_fx_source_not_approved"],
  notes: ["read-only"],
  cards: [
    {
      id: "ewy-kospi-implied-parity",
      type: "index_implied_parity",
      title: "EWY implied KOSPI parity",
      baseSymbol: "KOSPI",
      proxySymbol: "EWY",
      basePrice: 2900,
      proxyPrice: 72,
      fxRate: 1360,
      impliedValue: 2950,
      premiumPct: 1.72,
      tone: "premium",
      formula: "((proxyPrice * fxRate * divisor) / basePrice - 1) * 100",
      dataState: "fresh",
      source: {
        source: "fixture/naver+yahoo",
        sourceOfTruth: "approved_fixture_provider",
        asOf: "2026-05-14T00:00:00Z",
        stale: false,
        freshnessSec: 60,
        warnings: [],
      },
    },
    {
      id: "hyperliquid-smsn",
      type: "synthetic_kr_stock_parity",
      title: "삼성전자 synthetic parity",
      baseSymbol: "005930",
      syntheticSymbol: "xyz:SMSN",
      premiumPct: null,
      tone: "unknown",
      dataState: "disabled",
      emptyReason: "hyperliquid_source_not_approved",
      source: {
        source: "hyperliquid",
        sourceOfTruth: "approval_gate",
        asOf: null,
        stale: true,
        freshnessSec: null,
        warnings: ["hyperliquid_source_not_approved"],
      },
    },
  ],
};

test("renders market parity cards as reference observations, not recommendations", () => {
  render(<MarketParityStrip state={{ status: "ready", data: PAYLOAD }} />);

  expect(screen.getByText("괴리 참고")).toBeInTheDocument();
  expect(screen.getByText(/매수·매도 추천이 아닙니다/)).toBeInTheDocument();
  expect(screen.getByText("EWY implied KOSPI parity")).toBeInTheDocument();
  expect(screen.getByText("+1.72%")).toBeInTheDocument();
  expect(screen.getByText("fixture/naver+yahoo")).toBeInTheDocument();
  expect(screen.getByText(/일부 다리가 비어 있어/)).toBeInTheDocument();
  expect(screen.getByText(/hyperliquid_source_not_approved/)).toBeInTheDocument();
});

test("renders loading, empty, and error states", () => {
  const reload = vi.fn();
  const { rerender } = render(<MarketParityStrip state={{ status: "loading" }} reload={reload} />);
  expect(screen.getByTestId("market-parity-loading")).toBeInTheDocument();

  rerender(<MarketParityStrip state={{ status: "ready", data: { ...PAYLOAD, state: "missing", cards: [], emptyReason: "no approved parity legs" } }} />);
  expect(screen.getByText("no approved parity legs")).toBeInTheDocument();

  rerender(<MarketParityStrip state={{ status: "error", message: "/invest/api/market-parity 500" }} reload={reload} />);
  expect(screen.getByText(/일시적으로 불러오지 못했습니다/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "재시도" }));
  expect(reload).toHaveBeenCalledTimes(1);
});
