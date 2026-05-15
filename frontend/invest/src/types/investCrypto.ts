export type CryptoCapabilityState =
  | "supported"
  | "unavailable"
  | "reference_only"
  | "external_gap"
  | "deferred"
  | "read_only_mvp";

export type CryptoReferenceSourceState =
  | "available"
  | "cached"
  | "fixture"
  | "reference_only"
  | "stale"
  | "live"
  | "unavailable"
  | "error";

export type CryptoReferenceFreshness = "fresh" | "partial" | "stale" | "missing" | "fixture" | "live";

export type CryptoRiskBadgeKind =
  | "thin_orderbook"
  | "held"
  | "pending_order"
  | "data_unavailable"
  | "high_volatility"
  | "low_liquidity"
  | "candidate_watch"
  | "momentum_candidate";

export type CryptoRiskLevel = "low" | "medium" | "high" | "unknown";

export type CryptoCandidateReasonKind =
  | "momentum"
  | "liquidity"
  | "spread"
  | "watched"
  | "held"
  | "pending_order"
  | "data_quality";

export interface CryptoSourceState {
  source: string;
  state: CryptoCapabilityState;
  label: string;
  fetchedAt: string | null;
}

export interface CryptoCapabilityFlag {
  state: CryptoCapabilityState;
  reason: string | null;
}

export interface CryptoDashboardCapabilities {
  ticker: CryptoCapabilityFlag;
  candles: CryptoCapabilityFlag;
  orderbook: CryptoCapabilityFlag;
  recentTrades: CryptoCapabilityFlag;
  projectInfo: CryptoCapabilityFlag;
  liveStreaming: CryptoCapabilityFlag;
  execution: CryptoCapabilityFlag;
}

export interface CryptoRiskBadge {
  kind: CryptoRiskBadgeKind;
  label: string;
  severity: "info" | "warning" | "danger";
}

export interface CryptoRiskSummary {
  level: CryptoRiskLevel;
  score: number;
  reasons: string[];
}

export interface CryptoCandidateInsight {
  symbol: string;
  baseSymbol: string;
  displayName: string;
  rank: number;
  score: number;
  reasons: CryptoCandidateReasonKind[];
  summary: string;
  isHeld: boolean;
  isWatched: boolean;
  hasPendingOrder: boolean;
  riskLevel: CryptoRiskLevel;
}

export interface CryptoMarketCard {
  symbol: string;
  baseSymbol: string;
  displayName: string;
  priceKrw: number | null;
  changeRate24h: number | null;
  changeAmount24h: number | null;
  accTradePrice24h: number | null;
  volume24h: number | null;
  orderbookSpreadPct: number | null;
  isHeld: boolean;
  isWatched: boolean;
  badges: CryptoRiskBadge[];
  risk: CryptoRiskSummary | null;
}

export interface CryptoHoldingSummary {
  heldCount: number;
  symbols: string[];
  source: "invest_home_read_model";
}

export interface CryptoPendingOrderItem {
  orderId: string | null;
  symbol: string;
  baseSymbol: string | null;
  side: string;
  orderType: string | null;
  price: number | null;
  quantity: number;
  filledQuantity: number;
  status: string;
  orderedAt: string | null;
  updatedAt: string | null;
  source: "pending_orders";
}

export interface CryptoPendingOrdersSummary {
  items: CryptoPendingOrderItem[];
  emptyState: "no_pending_orders" | null;
  source: "pending_orders";
}

export interface CryptoInsightsSummary {
  badges: CryptoRiskBadge[];
  notes: string[];
  candidates: CryptoCandidateInsight[];
}

export interface CryptoDashboardResponse {
  asOf: string;
  market: "crypto";
  baseCurrency: "KRW";
  cards: CryptoMarketCard[];
  holdings: CryptoHoldingSummary | null;
  pendingOrders: CryptoPendingOrdersSummary | null;
  insights: CryptoInsightsSummary;
  capabilities: CryptoDashboardCapabilities;
  meta: { warnings: string[]; sources: CryptoSourceState[] };
}

export interface CryptoReferenceSourceMeta {
  source: string;
  label: string;
  state: CryptoReferenceSourceState;
  fetchedAt: string | null;
  cacheAgeSeconds: number | null;
  freshness: CryptoReferenceFreshness;
  errorCode: string | null;
  referenceOnly: boolean;
}

export interface NaverCryptoRankItem {
  rank: number;
  symbol: string;
  displayName: string;
  priceKrw: number | null;
  changeRate24h: number | null;
  tradeAmount24h: number | null;
  rsi: number | null;
  marketWarning: boolean | null;
  source: string;
}

export interface NaverCryptoProfile {
  symbol: string;
  baseSymbol: string;
  displayName: string;
  koreanName: string | null;
  englishName: string | null;
  naverUrl: string | null;
  officialMarket: string | null;
  referenceNotes: string[];
}

export interface NaverCryptoKimchiPremium {
  baseSymbol: string;
  premiumPct: number | null;
  domesticPriceKrw: number | null;
  overseasPriceKrw: number | null;
  state: CryptoReferenceSourceState;
  source: string;
  caution: string;
}

export interface NaverCryptoReferenceCapabilities {
  rank: CryptoCapabilityFlag;
  price: CryptoCapabilityFlag;
  profile: CryptoCapabilityFlag;
  news: CryptoCapabilityFlag;
  kimchiPremium: CryptoCapabilityFlag;
  execution: CryptoCapabilityFlag;
}

export interface NaverCryptoReferenceResponse {
  market: "crypto";
  asOf: string;
  symbol: string | null;
  rank: NaverCryptoRankItem[];
  profile: NaverCryptoProfile | null;
  news: { items: Array<{ id: number; title: string; publisher: string | null; url: string }> } | null;
  kimchiPremium: NaverCryptoKimchiPremium | null;
  sources: CryptoReferenceSourceMeta[];
  warnings: string[];
  capabilities: NaverCryptoReferenceCapabilities;
}
