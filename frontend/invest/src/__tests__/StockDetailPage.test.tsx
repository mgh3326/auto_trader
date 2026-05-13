import { render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, expect, test, vi } from "vitest";
import { StockDetailPage } from "../pages/stock-detail/StockDetailPage";
import * as stockApi from "../api/stockDetail";
import type {
  StockDetailCandlesResponse,
  StockDetailNewsResponse,
  StockDetailOrdersResponse,
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
    averageCost: 200,
    costBasis: 400,
    valueNative: 422.68,
    valueKrw: 575000,
    pnlKrw: 30000,
    pnlRate: 5.5,
    includedSources: ["kis"],
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

function renderPage() {
  return render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/stocks/us/QQQM"]}>
      <Routes>
        <Route path="/stocks/:market/:symbol" element={<StockDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.spyOn(stockApi, "fetchStockDetail").mockResolvedValue(aboveFold);
  vi.spyOn(stockApi, "fetchStockDetailCandles").mockResolvedValue(candles);
  vi.spyOn(stockApi, "fetchStockDetailOrders").mockResolvedValue(orders);
  vi.spyOn(stockApi, "fetchStockDetailNews").mockResolvedValue(news);
});

test("renders the QQQM stock detail shell from the read-only backend contract", async () => {
  renderPage();

  expect(await screen.findByTestId("stock-detail-shell")).toBeInTheDocument();
  expect(screen.getByRole("heading", { name: /Invesco NASDAQ 100 ETF/ })).toBeInTheDocument();
  expect(screen.getByText("QQQM · US · NASDAQ")).toBeInTheDocument();
  expect(screen.getByText("$211.34")).toBeInTheDocument();
  expect(screen.getByText("+1.06%")).toBeInTheDocument();
  expect(screen.getByTestId("stock-detail-holding")).toHaveTextContent("2주");
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
