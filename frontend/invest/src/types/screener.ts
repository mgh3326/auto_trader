export type ScreenerMarket = "kr" | "us" | "crypto";
export type ScreenerChangeDirection = "up" | "down" | "flat";
export type InvestorFlowChipTone =
  | "double_buy"
  | "double_sell"
  | "foreign_buy"
  | "foreign_sell"
  | "institution_buy"
  | "institution_sell"
  | "neutral";
export type InvestorFlowChipState = "fresh" | "stale" | "missing";

export interface ScreenerInvestorFlowChip {
  label: string;
  tone: InvestorFlowChipTone;
  dataState: InvestorFlowChipState;
  snapshotDate: string | null;
}

export interface ScreenerFilterChip {
  label: string;
  detail: string | null;
}

export type ScreenerPresetOrigin = "toss_parity" | "auto_trader_original";
export type ScreenerParityStatus = "full" | "partial" | "mismatch";

export interface ScreenerPreset {
  id: string;
  name: string;
  description: string;
  badges: string[];
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  market: ScreenerMarket;
  // ROB-359 Scope B (additive, optional during transition).
  presetOrigin?: ScreenerPresetOrigin;
  parityStatus?: ScreenerParityStatus | null;
  parityNote?: string | null;
}

export interface ScreenerPresetsResponse {
  presets: ScreenerPreset[];
  selectedPresetId: string | null;
}

export type ScreenerDataSourceKind =
  | "upbit_official"
  | "tvscreener_upbit"
  | "mcp_screen_stocks"
  | "naver_reference"
  | "coingecko_reference"
  | "external_reference"
  | "snapshot_cache";

export type ScreenerSourceState =
  | "supported"
  | "cached"
  | "reference_only"
  | "partial"
  | "unavailable"
  | "fallback";

export type ScreenerRiskSeverity = "info" | "warning" | "danger";

export interface ScreenerSourceContext {
  source: ScreenerDataSourceKind;
  label: string;
  state: ScreenerSourceState;
  fetchedAt: string | null;
  detail: string | null;
}

export interface ScreenerRiskContext {
  kind: string;
  label: string;
  severity: ScreenerRiskSeverity;
  source: ScreenerDataSourceKind | null;
}

export interface ScreenerCandidateContext {
  scoreLabel: string | null;
  reasons: string[];
  source: ScreenerDataSourceKind | null;
}

export interface ScreenerResultRow {
  rank: number;
  symbol: string;
  market: ScreenerMarket;
  name: string;
  logoUrl: string | null;
  isWatched: boolean;
  priceLabel: string;
  changePctLabel: string;
  changeAmountLabel: string;
  changeDirection: ScreenerChangeDirection;
  category: string;
  marketCapLabel: string;
  volumeLabel: string;
  analystLabel: string;
  metricValueLabel: string;
  investorFlowChip: ScreenerInvestorFlowChip | null;
  warnings: string[];
  sourceContext?: ScreenerSourceContext[];
  riskContext?: ScreenerRiskContext[];
  candidateContext?: ScreenerCandidateContext | null;
  // ROB-426 PR3
  marketCapSource?: "primary" | "fallback" | null;
}

export type ScreenerFreshnessSource = "live" | "cached" | "previous_session";
export type ScreenerDataState = "fresh" | "partial" | "stale" | "missing" | "fallback";

export type ScreenerDegradationReason =
  | "snapshot_missing"
  | "coverage_below_floor"
  | "older_fallback"
  | "healthy_no_matches"
  | "live";

export interface ScreenerFreshnessPrimary {
  kind: "screener_snapshot" | "live" | "fallback";
  snapshotDate: string | null;
  computedAt: string | null;
  asOfLabel: string;
  dataState: ScreenerDataState;
  source: string | null;
  // ROB-426 PR3
  degradationReason?: ScreenerDegradationReason | null;
  coverageLabel?: string | null;
}

export interface ScreenerFreshnessDependency {
  kind: "investor_flow";
  snapshotDate: string | null;
  collectedAt: string | null;
  lagLabel: string | null;
  dataState: ScreenerDataState;
  source: string | null;
}

export interface ScreenerFreshness {
  fetchedAt: string;
  asOfLabel: string;
  relativeLabel: string;
  cacheHit: boolean;
  source: ScreenerFreshnessSource;
  dataState: ScreenerDataState;
  // ROB-277 additive fields (optional during transition).
  servedAt?: string;
  servedRelativeLabel?: string;
  primary?: ScreenerFreshnessPrimary | null;
  dependencies?: ScreenerFreshnessDependency[];
  overallState?: ScreenerDataState;
}

export interface ScreenerResultsResponse {
  presetId: string;
  title: string;
  description: string;
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  results: ScreenerResultRow[];
  warnings: string[];
  freshness: ScreenerFreshness;
  sources?: ScreenerSourceContext[];
  // ROB-429 B2: full-partition predicate match total + returned (post-limit) count.
  // Populated only on the KR fundamentals presets; null/undefined elsewhere.
  // UI rendering is a follow-up — this is the type only.
  totalCount?: number | null;
  returnedCount?: number | null;
}
