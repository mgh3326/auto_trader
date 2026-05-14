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
export type FxSensitivityStatus =
  | "available"
  | "not_applicable"
  | "missing_holding"
  | "missing_native_value"
  | "missing_fx_rate";
export type FxSensitivityBasis = "portfolio_value" | "fallback_quote" | "not_applicable";
export type InvestorFlowDetailState = "fresh" | "stale" | "missing";

export interface StockDetailInvestorFlowDailyRow {
  snapshotDate: string;
  collectedAt: string | null;
  source: string | null;
  foreignNet: number | null;
  institutionNet: number | null;
  individualNet: number | null;
  doubleBuy: boolean;
  doubleSell: boolean;
}

export interface StockDetailInvestorFlow {
  source: "investor_flow_snapshots";
  market: "kr";
  symbol: string;
  dataState: InvestorFlowDetailState;
  snapshotDate: string | null;
  collectedAt: string | null;
  snapshotSource: string | null;
  foreignNet: number | null;
  institutionNet: number | null;
  individualNet: number | null;
  foreignNetBuyRank: number | null;
  foreignNetSellRank: number | null;
  institutionNetBuyRank: number | null;
  institutionNetSellRank: number | null;
  doubleBuy: boolean;
  doubleSell: boolean;
  foreignConsecutiveBuyDays: number | null;
  foreignConsecutiveSellDays: number | null;
  institutionConsecutiveBuyDays: number | null;
  institutionConsecutiveSellDays: number | null;
  individualConsecutiveBuyDays: number | null;
  individualConsecutiveSellDays: number | null;
  dailyRows: StockDetailInvestorFlowDailyRow[];
  cautionLabel: string;
}

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

export interface StockDetailFxScenario {
  rateMovePct: number;
  estimatedKrwImpact: number | null;
  estimatedValueKrw: number | null;
  label: string;
}

export interface StockDetailFxSensitivity {
  source: "stock_detail_fx_sensitivity";
  status: FxSensitivityStatus;
  currencyPair: "USD/KRW" | null;
  baseFxRate: number | null;
  holdingValueNative: number | null;
  holdingValueKrw: number | null;
  basis: FxSensitivityBasis;
  scenarios: StockDetailFxScenario[];
  caution: string;
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
  investorFlow: StockDetailInvestorFlow | null;
  holding: StockDetailHolding | null;
  fxSensitivity: StockDetailFxSensitivity | null;
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

export interface StockDetailResearchCitation {
  source: string;
  title: string | null;
  analyst: string | null;
  published_at_text?: string | null;
  published_at?: string | null;
  category: string | null;
  detail_url: string | null;
  pdf_url: string | null;
  excerpt: string | null;
  symbol_candidates: Array<{ symbol: string; market?: string | null; source?: string | null }>;
  attribution_publisher: string | null;
  attribution_copyright_notice: string | null;
}

export interface StockDetailAnalystConsensus {
  source: string | null;
  buyCount: number;
  holdCount: number;
  sellCount: number;
  strongBuyCount: number;
  totalCount: number;
  avgTargetPrice: number | null;
  medianTargetPrice: number | null;
  minTargetPrice: number | null;
  maxTargetPrice: number | null;
  upsidePct: number | null;
  currentPrice: number | null;
}

export interface StockDetailResearchFreshness {
  isReady: boolean;
  isStale: boolean;
  latestRunUuid: string | null;
  latestFinishedAt: string | null;
  latestReportCount: number;
  maxAgeHours: number;
}

export interface StockDetailResearchConsensusResponse {
  symbol: string;
  market: "kr" | "us";
  displayName: string;
  state: "ready" | "partial" | "missing" | "unsupported" | "error";
  dataState: "fresh" | "stale" | "missing" | "unsupported" | "error";
  emptyReason: "no_analyst_consensus_or_research_reports" | "market_unsupported" | "provider_error" | null;
  warnings: string[];
  sourceOfTruth: "analyst_opinions_and_research_reports" | "analyst_opinions" | "research_reports" | "none";
  asOf: string;
  stale: boolean;
  consensus: StockDetailAnalystConsensus | null;
  citations: StockDetailResearchCitation[];
  freshness: StockDetailResearchFreshness;
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
