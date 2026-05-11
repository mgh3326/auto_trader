import type { AccountSource, AssetCategory, AssetType, Currency, PriceState } from "./invest";
import type { FeedNewsResponse } from "./feedNews";

export type StockDetailMarket = "kr" | "us" | "crypto";
export type OrderbookUnsupportedReason = "us_unsupported" | "crypto_deferred" | "kr_unavailable";
export type CapabilityUnsupportedReason =
  | "read_only_mvp"
  | "out_of_mvp_scope"
  | "us_unsupported"
  | "crypto_deferred"
  | "unsupported_period";
export type ValuationFreshness = "ok" | "stale" | "unsupported" | "error";
export type ScreenerSnapshotFreshness = "fresh" | "stale" | "missing";
export type NaverPocStatus = "fixture_backed_poc" | "no_go";
export type NaverEndpointStatus =
  | "verified_200"
  | "verified_200_signal_only"
  | "page_candidate"
  | "needs_auth_or_contract_check"
  | "unsupported"
  | "error";
export type OrderSide = "buy" | "sell" | string;
export type AnalysisDecision = "buy" | "hold" | "sell";

export interface CapabilityFlag {
  supported: boolean;
  reason: CapabilityUnsupportedReason | OrderbookUnsupportedReason | string | null;
}

export interface CandleCapability {
  supported: boolean;
  intradaySupported: boolean;
}

export interface StockDetailCapabilities {
  candles: CandleCapability;
  orderbook: CapabilityFlag;
  news: CapabilityFlag;
  orders: CapabilityFlag;
  liveStreaming: CapabilityFlag;
  execution: CapabilityFlag;
  options: CapabilityFlag;
}

export interface StockDetailQuote {
  price: number | null;
  previousClose: number | null;
  changeAmount: number | null;
  changeRate: number | null;
  asOf: string | null;
  priceState: PriceState;
}

export interface StockDetailScreenerSnapshot {
  snapshotDate: string;
  consecutiveUpDays: number | null;
  weekChangeRate: number | null;
  dailyVolume: number | null;
  closesWindow: number[];
  source: string | null;
  freshness: ScreenerSnapshotFreshness;
}

export interface StockDetailValuation {
  per: number | null;
  pbr: number | null;
  roe: number | null;
  dividendYield: number | null;
  high52w: number | null;
  low52w: number | null;
  marketCap: number | null;
  source: string;
  asOf: string | null;
  freshness: ValuationFreshness;
}

export interface StockDetailNaverEndpointProbe {
  surface: string;
  url: string;
  status: NaverEndpointStatus;
  payloadFields: string[];
  mappedFields: string[];
  risk: string;
}

export interface StockDetailNaverEnrichment {
  source: "naver_stock_detail_poc";
  market: StockDetailMarket;
  symbol: string;
  naverCode: string;
  pageUrl: string;
  status: NaverPocStatus;
  liveFetchEnabled: boolean;
  endpoints: StockDetailNaverEndpointProbe[];
  usefulFields: string[];
  noGoFields: string[];
  docsPath: string;
}

export interface StockDetailHolding {
  totalQuantity: number;
  averageCost: number | null;
  costBasis: number | null;
  valueNative: number | null;
  valueKrw: number | null;
  pnlKrw: number | null;
  pnlRate: number | null;
  includedSources: AccountSource[];
  priceState: PriceState;
}

export interface StockDetailLatestAnalysis {
  id: number;
  modelName: string | null;
  decision: AnalysisDecision | null;
  confidence: number | null;
  appropriateBuyRange: [number | null, number | null] | null;
  appropriateSellRange: [number | null, number | null] | null;
  reasonsTop3: string[];
  createdAt: string | null;
}

export interface StockDetailOrderbookLevel {
  price: number;
  quantity: number;
}

export interface StockDetailOrderbook {
  asOf: string | null;
  asks: StockDetailOrderbookLevel[];
  bids: StockDetailOrderbookLevel[];
}

export interface StockDetailOrderbookSupport extends CapabilityFlag {
  reason: OrderbookUnsupportedReason | null;
}

export interface StockDetailResponse {
  symbol: string;
  market: StockDetailMarket;
  displayName: string;
  exchange: string;
  instrumentType: string;
  currency: Currency;
  assetType: AssetType;
  assetCategory: AssetCategory;
  quote: StockDetailQuote | null;
  screenerSnapshot: StockDetailScreenerSnapshot | null;
  valuation: StockDetailValuation | null;
  naverEnrichment: StockDetailNaverEnrichment | null;
  holding: StockDetailHolding | null;
  latestAnalysis: StockDetailLatestAnalysis | null;
  orderbookSupport: StockDetailOrderbookSupport;
  orderbook: StockDetailOrderbook | null;
  capabilities: StockDetailCapabilities;
  meta: { computedAt: string; warnings: string[] };
}

export interface StockDetailCandle {
  ts: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number | null;
}

export interface StockDetailCandlesResponse {
  symbol: string;
  market: StockDetailMarket;
  period: string;
  source: string;
  candles: StockDetailCandle[];
  capabilities: CandleCapability;
}

export type StockDetailNewsResponse = FeedNewsResponse;

export interface StockDetailOrder {
  orderId: string | null;
  symbol: string;
  market: StockDetailMarket;
  side: OrderSide;
  quantity: number;
  price: number | null;
  filledAt: string | null;
  account: string | null;
  source: string | null;
}

export interface StockDetailOrdersResponse {
  symbol: string;
  market: StockDetailMarket;
  items: StockDetailOrder[];
  nextCursor: string | null;
  meta: { emptyState: "no_filled_orders" | null; warnings: string[] };
}
