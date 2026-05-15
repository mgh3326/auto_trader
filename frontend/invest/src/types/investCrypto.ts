export type CryptoCapabilityState =
  | "supported"
  | "unavailable"
  | "reference_only"
  | "external_gap"
  | "deferred"
  | "read_only_mvp";

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
