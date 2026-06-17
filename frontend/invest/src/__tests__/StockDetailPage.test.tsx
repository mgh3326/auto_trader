import { render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
import { StockDetailPage } from "../pages/stock-detail/StockDetailPage";
import * as stockApi from "../api/stockDetail";
import * as watchApi from "../api/watches";
import { AccountPanelProvider } from "../desktop/AccountPanelProvider";
import { mockRightRail } from "../test/mockRightRail";
import type {
  StockDetailCandlesResponse,
  StockDetailNewsResponse,
  StockDetailOrdersResponse,
  StockDetailResearchConsensusResponse,
  StockDetailResponse,
} from "../types/stockDetail";

const aboveFold: StockDetailResponse = {
  symbol: "QQQM",
  market: "us",
  displayName: "Invesco NASDAQ 100 ETF",
  exchange: "NASDAQ",
  instrumentType: "ETF",
  currency: "USD",
  assetType: "equity",
  assetCategory: "us_stock",
  quote: {
    price: 211.34,
    previousClose: 209.12,
    changeAmount: 2.22,
    changeRate: 1.061,
    asOf: "2026-05-10T09:30:00Z",
    priceState: "live",
  },
  screenerSnapshot: {
    snapshotDate: "2026-05-09",
    consecutiveUpDays: 5,
    weekChangeRate: 3.4,
    dailyVolume: 1234567,
    closesWindow: [201, 205, 207, 209, 211],
    source: "invest_screener_snapshots",
    freshness: "fresh",
  },
  valuation: {
    per: null,
    pbr: null,
    roe: null,
    dividendYield: 0.64,
    high52w: 213.5,
    low52w: 156.2,
    marketCap: null,
    source: "db",
    asOf: "2026-05-09T00:00:00Z",
    freshness: "ok",
  },
  investorFlow: null,
  naverEnrichment: {
    source: "naver_stock_detail_poc",
    market: "us",
    symbol: "QQQM",
    naverCode: "QQQM.O",
    pageUrl: "https://stock.naver.com/worldstock/stock/QQQM.O/price",
    status: "fixture_backed_poc",
    liveFetchEnabled: false,
    endpoints: [
      {
        surface: "worldstock_price_polling",
        url: "https://stock.naver.com/api/polling/worldstock/stock?reutersCodes=QQQM.O",
        status: "verified_200",
        payloadFields: ["datas[].closePrice"],
        mappedFields: ["quote.price"],
        risk: "Do not poll without approval.",
      },
      {
        surface: "worldstock_finance_overview",
        url: "https://stock.naver.com/worldstock/stock/QQQM.O/finance/overview",
        status: "page_candidate",
        payloadFields: ["financial rows"],
        mappedFields: ["valuation"],
        risk: "Needs contract discovery.",
      },
    ],
    usefulFields: ["source freshness / polling interval", "valuation/profile rows", "related news citation metadata"],
    noGoFields: ["raw public discussion post text", "scheduled polling/backfill without explicit approval"],
    docsPath: "docs/invest/naver-stock-detail-raw-data-poc.md",
  },
  holding: {
    totalQuantity: 2,
    tradeableQuantity: 1,
    sellableQuantity: 1,
    pendingSellQuantity: 0,
    referenceQuantity: 1,
    averageCost: 200,
    costBasis: 400,
    valueNative: 422.68,
    valueKrw: 575000,
    pnlKrw: 30000,
    pnlRate: 0.055,
    includedSources: ["kis", "toss_manual"],
    priceState: "live",
  },
  fxSensitivity: {
    source: "stock_detail_fx_sensitivity",
    status: "available",
    currencyPair: "USD/KRW",
    baseFxRate: 1360,
    holdingValueNative: 422.68,
    holdingValueKrw: 575000,
    basis: "portfolio_value",
    scenarios: [
      {
        rateMovePct: -1,
        estimatedKrwImpact: -5748.448,
        estimatedValueKrw: 569096.352,
        label: "USD/KRW -1%",
      },
      {
        rateMovePct: 1,
        estimatedKrwImpact: 5748.448,
        estimatedValueKrw: 580593.248,
        label: "USD/KRW +1%",
      },
    ],
    caution: "환율 민감도는 USD/KRW 1% 변동을 보유 평가액에 단순 적용한 가정치입니다.",
  },
  latestAnalysis: {
    id: 11,
    modelName: "committee",
    decision: "hold",
    confidence: 0.72,
    appropriateBuyRange: [195, 205],
    appropriateSellRange: [220, 230],
    reasonsTop3: ["나스닥100 분산", "최근 5일 상승", "밸류에이션은 중립"],
    createdAt: "2026-05-09T12:00:00Z",
  },
  orderbookSupport: { supported: false, reason: "us_unsupported" },
  orderbook: null,
  cryptoDetail: null,
  capabilities: {
    candles: { supported: true, intradaySupported: true },
    orderbook: { supported: false, reason: "us_unsupported" },
    news: { supported: true, reason: null },
    orders: { supported: true, reason: null },
    liveStreaming: { supported: false, reason: "out_of_mvp_scope" },
    execution: { supported: false, reason: "read_only_mvp" },
    options: { supported: false, reason: "out_of_mvp_scope" },
  },
  meta: { computedAt: "2026-05-10T09:31:00Z", warnings: [] },
};

const candles: StockDetailCandlesResponse = {
  symbol: "QQQM",
  market: "us",
  period: "1d",
  source: "db",
  capabilities: { supported: true, intradaySupported: true },
  candles: [
    { ts: "2026-05-06T00:00:00Z", open: 201, high: 203, low: 200, close: 202, volume: 1000 },
    { ts: "2026-05-07T00:00:00Z", open: 202, high: 206, low: 201, close: 205, volume: 2000 },
    { ts: "2026-05-08T00:00:00Z", open: 205, high: 210, low: 204, close: 209, volume: 2500 },
  ],
};

const orders: StockDetailOrdersResponse = {
  symbol: "QQQM",
  market: "us",
  items: [],
  nextCursor: null,
  meta: { emptyState: "no_filled_orders", warnings: [] },
};

const news: StockDetailNewsResponse = {
  tab: "top",
  asOf: "2026-05-10T09:31:00Z",
  issues: [],
  items: [
    {
      id: 44,
      title: "QQQM tracks Nasdaq rally",
      market: "us",
      sourceMarket: "us",
      relatedSymbols: [{ symbol: "QQQM", market: "us", displayName: "QQQM", relation: "held" }],
      relation: "held",
      url: "https://example.com/qqqm",
      publisher: "Reuters",
      feedSource: null,
      publishedAt: "2026-05-10T08:00:00Z",
      issueId: null,
      summarySnippet: "QQQM rose with mega-cap technology stocks.",
    },
  ],
  meta: { warnings: [] },
};

const researchConsensus: StockDetailResearchConsensusResponse = {
  symbol: "QQQM",
  market: "us",
  displayName: "Invesco NASDAQ 100 ETF",
  state: "ready",
  dataState: "fresh",
  emptyReason: null,
  warnings: [],
  sourceOfTruth: "analyst_opinions_and_research_reports",
  asOf: "2026-05-10T09:31:00Z",
  stale: false,
  consensus: {
    source: "yfinance",
    buyCount: 7,
    holdCount: 2,
    sellCount: 0,
    strongBuyCount: 3,
    totalCount: 9,
    avgTargetPrice: 235,
    medianTargetPrice: 236,
    minTargetPrice: 220,
    maxTargetPrice: 250,
    upsidePct: 11.2,
    currentPrice: 211.34,
  },
  citations: [
    {
      source: "issuer_ir",
      title: "QQQM holdings note",
      analyst: "ETF Desk",
      published_at: "2026-05-10T08:30:00Z",
      category: "ETF",
      detail_url: "https://example.com/research/qqqm",
      pdf_url: null,
      excerpt: "Nasdaq 100 구성 종목 변화와 기술주 집중도를 요약합니다.",
      symbol_candidates: [{ symbol: "QQQM", market: "us", source: "ticker" }],
      attribution_publisher: "Issuer",
      attribution_copyright_notice: "metadata only",
    },
  ],
  freshness: {
    isReady: true,
    isStale: false,
    latestRunUuid: "run-1",
    latestFinishedAt: "2026-05-10T09:00:00Z",
    latestReportCount: 1,
    maxAgeHours: 24,
  },
};

function renderPage(path = "/invest/stocks/us/QQQM") {
  return render(
    <AccountPanelProvider>
      <MemoryRouter basename="/invest" initialEntries={[path]}>
        <Routes>
          <Route path="/stocks/:market/:symbol" element={<StockDetailPage />} />
        </Routes>
      </MemoryRouter>
    </AccountPanelProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  mockRightRail();
  vi.spyOn(stockApi, "fetchStockDetail").mockResolvedValue(aboveFold);
  vi.spyOn(stockApi, "fetchStockDetailCandles").mockResolvedValue(candles);
  vi.spyOn(stockApi, "fetchStockDetailOrders").mockResolvedValue(orders);
  vi.spyOn(stockApi, "fetchStockDetailNews").mockResolvedValue(news);
  vi.spyOn(stockApi, "fetchStockDetailResearchConsensus").mockResolvedValue(researchConsensus);
  vi.spyOn(watchApi, "fetchWatches").mockResolvedValue({
    market: "us",
    status: "all",
    count: 0,
    data_state: "ok",
    as_of: "2026-05-10T09:31:00Z",
    items: [],
    warnings: [],
    empty_reason: null,
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

test("renders the QQQM stock detail shell from the read-only backend contract", async () => {
  renderPage();

  expect(await screen.findByTestId("stock-detail-shell")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /Invesco NASDAQ 100 ETF/ })).toBeInTheDocument();
  expect(screen.getByText("QQQM · US · NASDAQ")).toBeInTheDocument();
  expect(screen.getByText("$211.34")).toBeInTheDocument();
  expect(screen.getByText("+1.06%")).toBeInTheDocument();
  expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("2주");
  expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("KIS");
  expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("Toss");
  expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("매매가능 1주");
  expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("참고 1주");
  expect(screen.getByTestId("stock-detail-fx-sensitivity")).toHaveTextContent("환율 민감도");
  expect(screen.getByTestId("stock-detail-fx-sensitivity")).toHaveTextContent("USD/KRW");
  expect(screen.getByTestId("stock-detail-fx-sensitivity")).toHaveTextContent("+₩5,748");
  expect(screen.getByTestId("stock-detail-profile")).toHaveTextContent("ETF");
  expect(screen.getByTestId("stock-detail-analysis")).toHaveTextContent("hold");
  expect(screen.getByTestId("stock-detail-naver-poc")).toHaveTextContent("Naver 원천 데이터 PoC");
  expect(screen.getByTestId("stock-detail-naver-poc")).toHaveTextContent("live fetch off");

  await waitFor(() => expect(stockApi.fetchStockDetailCandles).toHaveBeenCalledWith({ market: "us", symbol: "QQQM", period: "1d" }));
  expect(await screen.findByTestId("stock-detail-chart")).toHaveTextContent("3개 캔들");
  expect(await screen.findByTestId("stock-detail-news")).toHaveTextContent("QQQM tracks Nasdaq rally");
});

test("renders research consensus ready state with analyst metrics and compact citations", async () => {
  renderPage();

  const card = await screen.findByTestId("stock-detail-research-consensus");
  expect(card).toHaveTextContent("리서치 · 컨센서스");
  expect(card).toHaveTextContent("최신");
  expect(card).toHaveTextContent("Buy");
  expect(card).toHaveTextContent("7/9");
  expect(card).toHaveTextContent("QQQM holdings note");
  expect(card).toHaveTextContent("ETF Desk");
  expect(card).not.toHaveTextContent("raw_payload");
  await waitFor(() => expect(stockApi.fetchStockDetailResearchConsensus).toHaveBeenCalledWith({ market: "us", symbol: "QQQM" }));
});

test("renders research consensus empty state when no opinions or citations are available", async () => {
  vi.mocked(stockApi.fetchStockDetailResearchConsensus).mockResolvedValue({
    ...researchConsensus,
    state: "missing",
    dataState: "missing",
    emptyReason: "no_analyst_consensus_or_research_reports",
    sourceOfTruth: "none",
    consensus: null,
    citations: [],
  });

  renderPage();

  const card = await screen.findByTestId("stock-detail-research-consensus");
  expect(card).toHaveTextContent("데이터 없음");
  expect(card).toHaveTextContent("애널리스트 컨센서스와 리서치 인용이 없습니다.");
});

test("renders research consensus stale and warning state", async () => {
  vi.mocked(stockApi.fetchStockDetailResearchConsensus).mockResolvedValue({
    ...researchConsensus,
    state: "partial",
    dataState: "stale",
    stale: true,
    warnings: ["research_reports_stale"],
    sourceOfTruth: "research_reports",
    consensus: null,
    freshness: { ...researchConsensus.freshness, isReady: false, isStale: true },
  });

  renderPage();

  const card = await screen.findByTestId("stock-detail-research-consensus");
  expect(card).toHaveTextContent("오래된 데이터");
  expect(card).toHaveTextContent("컨센서스 없이 리서치 인용만 표시합니다.");
  expect(card).toHaveTextContent("경고: research_reports_stale");
});

test("renders research consensus fetch error state without blocking the page", async () => {
  vi.mocked(stockApi.fetchStockDetailResearchConsensus).mockRejectedValue(new Error("offline"));

  renderPage();

  expect(await screen.findByTestId("stock-detail-shell")).toBeInTheDocument();
  expect(await screen.findByTestId("stock-detail-research-consensus")).toHaveTextContent("리서치 데이터를 사용할 수 없습니다.");
});

test("does not fetch or render research consensus for crypto symbols", async () => {
  vi.mocked(stockApi.fetchStockDetail).mockResolvedValue({
    ...aboveFold,
    symbol: "KRW-BTC",
    market: "crypto",
    displayName: "Bitcoin",
    exchange: "UPBIT",
    currency: "KRW",
    assetCategory: "crypto",
    naverEnrichment: null,
    fxSensitivity: null,
  });

  renderPage("/invest/stocks/crypto/KRW-BTC");

  expect(await screen.findByTestId("stock-detail-shell")).toBeInTheDocument();
  expect(stockApi.fetchStockDetailResearchConsensus).not.toHaveBeenCalled();
  expect(screen.queryByTestId("stock-detail-research-consensus")).not.toBeInTheDocument();
});

test("keeps buy/sell controls disabled and shows explicit orderbook plus empty order history states", async () => {
  renderPage();

  const buy = await screen.findByRole("button", { name: "매수 준비중" });
  const sell = screen.getByRole("button", { name: "매도 준비중" });
  expect(buy).toBeDisabled();
  expect(sell).toBeDisabled();
  expect(screen.getByTestId("stock-detail-trade-guardrail")).toHaveTextContent("read_only_mvp");
  expect(screen.getByTestId("stock-detail-orderbook")).toHaveTextContent("US 호가는 아직 지원하지 않습니다");
  expect(await screen.findByTestId("stock-detail-orders")).toHaveTextContent("체결 내역이 없습니다");
});

test("renders the upgraded fill table (buy/sell · price · notional) and the per-symbol watch card", async () => {
  vi.mocked(stockApi.fetchStockDetailOrders).mockResolvedValue({
    symbol: "QQQM",
    market: "us",
    items: [
      { orderId: "o1", symbol: "QQQM", market: "us", side: "buy", quantity: 2, price: 200, filledAt: "2026-05-09T13:30:00Z", account: "kis", source: "reconciler" },
      { orderId: "o2", symbol: "QQQM", market: "us", side: "sell", quantity: 3, price: 210, filledAt: "2026-05-10T13:30:00Z", account: "kis", source: "websocket" },
    ],
    nextCursor: null,
    meta: { emptyState: null, warnings: [] },
  });
  vi.mocked(watchApi.fetchWatches).mockResolvedValue({
    market: "us",
    status: "all",
    count: 1,
    data_state: "ok",
    as_of: "2026-05-10T09:31:00Z",
    items: [
      {
        alert_uuid: "a1",
        source_report_uuid: "r1",
        market: "us",
        symbol: "QQQM",
        symbol_name: "Invesco NASDAQ 100 ETF",
        target_kind: "asset",
        metric: "price_above",
        operator: "above",
        threshold: "230",
        threshold_high: null,
        status: "active",
        valid_until: "2026-05-20T00:00:00Z",
        intent: "sell_review",
        action_mode: "notify_only",
        rationale: "목표가 근접 시 분할 매도 검토",
        trigger_checklist: [],
        max_action: {},
        current_price: "211.34",
        proximity_band: "within_1_pct",
        last_event: null,
        near_expiry: false,
      },
    ],
    warnings: [],
    empty_reason: null,
  });

  renderPage();

  const ordersCard = within(await screen.findByTestId("stock-detail-orders"));
  expect(ordersCard.getByText("일시")).toBeInTheDocument();
  expect(ordersCard.getByText("구분")).toBeInTheDocument();
  expect(ordersCard.getByText("총액")).toBeInTheDocument();
  expect(ordersCard.getByText("매수")).toBeInTheDocument();
  expect(ordersCard.getByText("매도")).toBeInTheDocument();
  // notional = price × quantity, formatted for USD (buy 2×200, sell 3×210)
  expect(ordersCard.getByText("$400.00")).toBeInTheDocument();
  expect(ordersCard.getByText("$630.00")).toBeInTheDocument();
  expect(ordersCard.getByText("보정")).toBeInTheDocument();
  expect(ordersCard.getByText("실시간")).toBeInTheDocument();

  const watchCard = within(await screen.findByTestId("stock-detail-watch"));
  expect(watchCard.getByText("감시중")).toBeInTheDocument();
  expect(watchCard.getByText(/가격 \$230.00 이상/)).toBeInTheDocument();
  expect(watchCard.getByText("목표가 근접 시 분할 매도 검토")).toBeInTheDocument();
  await waitFor(() => expect(watchApi.fetchWatches).toHaveBeenCalledWith("us", "all", "QQQM"));
});

test("omits external community clone and uses a local memo placeholder", async () => {
  renderPage();

  expect(await screen.findByTestId("stock-detail-memo-placeholder")).toHaveTextContent("메모");
  expect(screen.queryByText(/커뮤니티/)).not.toBeInTheDocument();
});

test("omits the Naver PoC card when the backend has no safe enrichment map", async () => {
  vi.mocked(stockApi.fetchStockDetail).mockResolvedValue({ ...aboveFold, naverEnrichment: null });

  renderPage();

  expect(await screen.findByTestId("stock-detail-shell")).toBeInTheDocument();
  expect(screen.queryByTestId("stock-detail-naver-poc")).not.toBeInTheDocument();
});

test("renders KR investor-flow summary and daily rows as read-only reference data", async () => {
  vi.mocked(stockApi.fetchStockDetail).mockResolvedValue({
    ...aboveFold,
    market: "kr",
    symbol: "403550",
    displayName: "쏘카",
    exchange: "KOSPI",
    currency: "KRW",
    investorFlow: {
      source: "investor_flow_snapshots",
      market: "kr",
      symbol: "403550",
      dataState: "fresh",
      snapshotDate: "2026-05-13",
      collectedAt: "2026-05-13T15:40:00Z",
      snapshotSource: "naver_finance",
      foreignNet: 20859,
      institutionNet: -12931,
      individualNet: 125586,
      foreignNetBuyRank: null,
      foreignNetSellRank: null,
      institutionNetBuyRank: null,
      institutionNetSellRank: null,
      doubleBuy: false,
      doubleSell: false,
      foreignConsecutiveBuyDays: 4,
      foreignConsecutiveSellDays: null,
      institutionConsecutiveBuyDays: null,
      institutionConsecutiveSellDays: 1,
      individualConsecutiveBuyDays: 2,
      individualConsecutiveSellDays: null,
      dailyRows: [
        {
          snapshotDate: "2026-05-13",
          collectedAt: "2026-05-13T15:40:00Z",
          source: "naver_finance",
          close: null,
          changeRate: null,
          volume: null,
          foreignNet: 20859,
          foreignHoldingShares: null,
          foreignHoldingRate: null,
          institutionNet: -12931,
          individualNet: 125586,
          doubleBuy: false,
          doubleSell: false,
        },
        {
          snapshotDate: "2026-05-12",
          collectedAt: "2026-05-12T15:40:00Z",
          source: "naver_finance",
          close: null,
          changeRate: null,
          volume: null,
          foreignNet: 440,
          foreignHoldingShares: null,
          foreignHoldingRate: null,
          institutionNet: 1024,
          individualNet: -1464,
          doubleBuy: true,
          doubleSell: false,
        },
      ],
      periodSummary: {
        windowDays: 2,
        rowCount: 2,
        foreignNetTotal: 21299,
        institutionNetTotal: -11907,
        individualNetTotal: 124122,
        foreignBuyDays: 2,
        foreignSellDays: 0,
        foreignFlatDays: 0,
        foreignNetToVolumeRatio: null,
        foreignHoldingSharesChange: null,
        foreignHoldingRateChange: null,
        unavailableLabels: ["거래량 저장 전까지 계산 불가"],
      },
      buyerDecomposition: {
        snapshotDate: "2026-05-13",
        label: "개인 주도",
        leadingBuyer: "individual",
        foreignNet: 20859,
        institutionNet: -12931,
        individualNet: 125586,
        note: "최신 수급 행 기준입니다.",
      },
      unavailableLabels: ["외국인 순매수/거래량 강도: 거래량 저장 전까지 계산 불가"],
      cautionLabel: "투자자별 수급은 지연된 과거 참고 데이터이며 매매 판단을 대신하지 않습니다.",
    },
  });

  renderPage();

  const card = await screen.findByTestId("stock-detail-investor-flow");
  expect(card).toHaveTextContent("투자자별 매매동향 · 수급 흐름");
  expect(card).toHaveTextContent("naver_finance · 기준일 2026-05-13");
  expect(card).toHaveTextContent("+20,859주");
  expect(card).toHaveTextContent("−12,931주");
  expect(card).toHaveTextContent("최근 2거래일 수급 요약");
  expect(card).toHaveTextContent("주도 매수자 분해");
  expect(card).toHaveTextContent("개인 주도");
  expect(card).toHaveTextContent("2026-05-12");
  expect(card).toHaveTextContent("쌍끌이");
  expect(card).toHaveTextContent("매매 판단을 대신하지 않습니다");
});


test("renders conservative fallback text when FX sensitivity is not applicable", async () => {
  vi.mocked(stockApi.fetchStockDetail).mockResolvedValue({
    ...aboveFold,
    market: "kr",
    symbol: "005930",
    displayName: "삼성전자",
    exchange: "KOSPI",
    currency: "KRW",
    fxSensitivity: {
      source: "stock_detail_fx_sensitivity",
      status: "not_applicable",
      currencyPair: null,
      baseFxRate: null,
      holdingValueNative: null,
      holdingValueKrw: null,
      basis: "not_applicable",
      scenarios: [],
      caution: "KRW 자산은 별도 USD/KRW 환율 민감도 계산을 표시하지 않습니다.",
    },
  });

  renderPage();

  expect(await screen.findByTestId("stock-detail-fx-sensitivity")).toHaveTextContent("환율 민감도");
  expect(screen.getByTestId("stock-detail-fx-sensitivity")).toHaveTextContent("KRW 자산은 별도 USD/KRW 환율 민감도 계산을 표시하지 않습니다.");
});

test("omits the FX sensitivity card when the backend returns null", async () => {
  vi.mocked(stockApi.fetchStockDetail).mockResolvedValue({ ...aboveFold, fxSensitivity: null });

  renderPage();

  expect(await screen.findByTestId("stock-detail-shell")).toBeInTheDocument();
  expect(screen.queryByTestId("stock-detail-fx-sensitivity")).not.toBeInTheDocument();
});
