import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../desktop/RightRemotePanel", () => ({
  RightRemotePanel: () => <aside data-testid="right-remote-panel" />,
}));

import { DesktopCryptoPage } from "../pages/desktop/DesktopCryptoPage";
import * as cryptoApi from "../api/investCrypto";
import type { CryptoDashboardResponse } from "../types/investCrypto";

const dashboard: CryptoDashboardResponse = {
  asOf: "2026-05-13T12:00:00Z",
  market: "crypto",
  baseCurrency: "KRW",
  cards: [
    {
      symbol: "KRW-BTC",
      baseSymbol: "BTC",
      displayName: "비트코인",
      priceKrw: 101000000,
      changeRate24h: 0.0123,
      changeAmount24h: 1230000,
      accTradePrice24h: 12345678900,
      volume24h: 234.5,
      orderbookSpreadPct: 0.62,
      isHeld: true,
      isWatched: true,
      badges: [
        { kind: "held", label: "보유", severity: "info" },
        { kind: "pending_order", label: "미체결", severity: "warning" },
      ],
    },
  ],
  holdings: { heldCount: 1, symbols: ["KRW-BTC"], source: "invest_home_read_model" },
  pendingOrders: {
    items: [
      {
        orderId: "upbit-open-1",
        symbol: "KRW-BTC",
        baseSymbol: "BTC",
        side: "buy",
        orderType: "limit",
        price: 100000000,
        quantity: 0.01,
        filledQuantity: 0,
        status: "open",
        orderedAt: "2026-05-13T12:00:00Z",
        updatedAt: "2026-05-13T12:01:00Z",
        source: "pending_orders",
      },
    ],
    emptyState: null,
    source: "pending_orders",
  },
  insights: { badges: [], notes: ["읽기 전용"] },
  capabilities: {
    ticker: { state: "supported", reason: null },
    candles: { state: "supported", reason: null },
    orderbook: { state: "supported", reason: null },
    recentTrades: { state: "external_gap", reason: "upbit_public_dashboard_mvp" },
    projectInfo: { state: "reference_only", reason: "external_reference_only" },
    liveStreaming: { state: "deferred", reason: null },
    execution: { state: "read_only_mvp", reason: null },
  },
  meta: { warnings: [], sources: [{ source: "upbit_ticker", state: "supported", label: "Upbit ticker", fetchedAt: "2026-05-13T12:00:00Z" }] },
};

beforeEach(() => {
  vi.spyOn(cryptoApi, "fetchCryptoDashboard").mockResolvedValue(dashboard);
});

test("renders crypto dashboard cards and read-only capability states", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/crypto"]}>
      <DesktopCryptoPage />
    </MemoryRouter>,
  );

  expect(await screen.findByTestId("crypto-dashboard")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "크립토 대시보드" })).toBeInTheDocument();
  expect(screen.getByText("비트코인")).toBeInTheDocument();
  expect(screen.getByText("101,000,000원")).toBeInTheDocument();
  expect(screen.getByText("보유")).toBeInTheDocument();
  expect(screen.getByText("미체결")).toBeInTheDocument();
  expect(screen.getByText(/주문·감시·동기화 작업은 실행하지 않습니다/)).toBeInTheDocument();
  expect(screen.getByText(/체결\/주문 실행: read_only_mvp/)).toBeInTheDocument();
});
