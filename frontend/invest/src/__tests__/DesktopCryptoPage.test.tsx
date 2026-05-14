import { render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";

vi.mock("../desktop/RightRemotePanel", () => ({
  RightRemotePanel: () => <aside data-testid="right-remote-panel" />,
}));

import { DesktopCryptoPage } from "../pages/desktop/DesktopCryptoPage";
import * as cryptoApi from "../api/investCrypto";
import type { CryptoDashboardResponse, NaverCryptoReferenceResponse } from "../types/investCrypto";

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
        { kind: "high_volatility", label: "변동성 주의", severity: "warning" },
        { kind: "low_liquidity", label: "거래대금 낮음", severity: "warning" },
        { kind: "candidate_watch", label: "관심 후보", severity: "info" },
      ],
      risk: {
        level: "medium",
        score: 35,
        reasons: ["호가 스프레드 확대", "미체결 상태 존재"],
      },
    },
    {
      symbol: "KRW-ETH",
      baseSymbol: "ETH",
      displayName: "이더리움",
      priceKrw: 5200000,
      changeRate24h: -0.01,
      changeAmount24h: -52000,
      accTradePrice24h: 800000000,
      volume24h: 1000,
      orderbookSpreadPct: 0.2,
      isHeld: false,
      isWatched: false,
      badges: [],
      risk: { level: "low", score: 0, reasons: [] },
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
  insights: {
    badges: [],
    notes: ["읽기 전용"],
    candidates: [
      {
        symbol: "KRW-BTC",
        baseSymbol: "BTC",
        displayName: "비트코인",
        rank: 1,
        score: 55,
        reasons: ["watched", "liquidity", "spread"],
        summary: "기존 검토 목록과 일치 · 거래대금 양호",
        isHeld: true,
        isWatched: true,
        hasPendingOrder: true,
        riskLevel: "medium",
      },
    ],
  },
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

const reference: NaverCryptoReferenceResponse = {
  market: "crypto",
  asOf: "2026-05-13T12:01:00Z",
  symbol: "KRW-BTC",
  rank: [
    {
      rank: 1,
      symbol: "KRW-BTC",
      displayName: "비트코인",
      priceKrw: 101000000,
      changeRate24h: 0.0123,
      tradeAmount24h: 12345678900,
      rsi: 55.5,
      marketWarning: false,
      source: "tvscreener_upbit",
    },
  ],
  profile: {
    symbol: "KRW-BTC",
    baseSymbol: "BTC",
    displayName: "비트코인",
    koreanName: "비트코인",
    englishName: "Bitcoin",
    naverUrl: "https://m.stock.naver.com/crypto/UPBIT/KRW-BTC",
    officialMarket: "UPBIT/KRW",
    referenceNotes: ["reference-only"],
  },
  news: { items: [{ id: 1, title: "BTC reference news", publisher: "fixture", url: "https://example.test/btc" }] },
  kimchiPremium: {
    baseSymbol: "BTC",
    premiumPct: 2.5,
    domesticPriceKrw: 101000000,
    overseasPriceKrw: 98500000,
    state: "available",
    source: "mcp_kimchi_premium",
    caution: "참고용 매크로 지표",
  },
  sources: [
    {
      source: "naver_reference",
      label: "Naver crypto reference fixture",
      state: "reference_only",
      fetchedAt: null,
      cacheAgeSeconds: null,
      freshness: "fixture",
      errorCode: null,
      referenceOnly: true,
    },
  ],
  warnings: ["naver_crypto_reference_only"],
  capabilities: {
    rank: { state: "supported", reason: null },
    price: { state: "supported", reason: null },
    profile: { state: "reference_only", reason: "naver_fixture_reference_only" },
    news: { state: "supported", reason: null },
    kimchiPremium: { state: "reference_only", reason: "macro_reference_only" },
    execution: { state: "read_only_mvp", reason: "no_order_execution_controls" },
  },
};

beforeEach(() => {
  vi.spyOn(cryptoApi, "fetchCryptoDashboard").mockResolvedValue(dashboard);
  vi.spyOn(cryptoApi, "fetchCryptoNaverReference").mockResolvedValue(reference);
});

test("renders crypto dashboard cards and read-only capability states", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/crypto"]}>
      <DesktopCryptoPage />
    </MemoryRouter>,
  );

  expect(await screen.findByTestId("crypto-dashboard")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: "크립토 대시보드" })).toBeInTheDocument();
  expect(screen.getAllByText("비트코인").length).toBeGreaterThan(0);
  expect(screen.getByText("101,000,000원")).toBeInTheDocument();
  expect(screen.getByText("보유")).toBeInTheDocument();
  expect(screen.getByText("미체결")).toBeInTheDocument();
  expect(screen.getByText(/주문·감시·동기화 작업은 실행하지 않습니다/)).toBeInTheDocument();
  expect(screen.getByText(/체결\/주문 실행: read_only_mvp/)).toBeInTheDocument();
  expect(await screen.findByRole("heading", { name: "Naver 참고 지표" })).toBeInTheDocument();
  expect(screen.getByText("2.50%")).toBeInTheDocument();
  expect(screen.getByText("BTC reference news")).toBeInTheDocument();
  expect(screen.getByText(/Naver crypto reference fixture: reference_only/)).toBeInTheDocument();
  expect(cryptoApi.fetchCryptoNaverReference).toHaveBeenCalledWith({ symbol: "KRW-BTC", limit: 20 });
});

test("renders risk summary, card risk labels, and candidate insight rows", async () => {
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/crypto"]}>
      <DesktopCryptoPage />
    </MemoryRouter>,
  );

  const summary = await screen.findByLabelText("리스크 요약");
  expect(within(summary).getByText("중간 1")).toBeInTheDocument();
  expect(within(summary).getByText("낮음 1")).toBeInTheDocument();
  expect(screen.getByText(/리스크 중간 · 35/)).toBeInTheDocument();
  expect(screen.getByText("변동성 주의")).toBeInTheDocument();
  expect(screen.getByText("거래대금 낮음")).toBeInTheDocument();
  expect(screen.getByText("관심 후보")).toBeInTheDocument();

  const candidates = screen.getByLabelText("후보 인사이트");
  expect(within(candidates).getByText("1. 비트코인")).toBeInTheDocument();
  expect(within(candidates).getByText(/후보 인사이트는 참고용/)).toBeInTheDocument();
  expect(within(candidates).getByText("검토 목록")).toBeInTheDocument();
  expect(within(candidates).getByText("유동성")).toBeInTheDocument();
  expect(within(candidates).queryByRole("button", { name: /주문|매수|매도|실행|등록/ })).not.toBeInTheDocument();
});

test("renders empty candidate insight state", async () => {
  vi.spyOn(cryptoApi, "fetchCryptoDashboard").mockResolvedValue({
    ...dashboard,
    insights: { ...dashboard.insights, candidates: [] },
  });

  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/crypto"]}>
      <DesktopCryptoPage />
    </MemoryRouter>,
  );

  expect(await screen.findByText("조건에 맞는 후보 인사이트가 없습니다.")).toBeInTheDocument();
});
