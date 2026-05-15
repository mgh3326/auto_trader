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

export interface ScreenerPreset {
  id: string;
  name: string;
  description: string;
  badges: string[];
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  market: ScreenerMarket;
}

export interface ScreenerPresetsResponse {
  presets: ScreenerPreset[];
  selectedPresetId: string | null;
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
}

export type ScreenerFreshnessSource = "live" | "cached" | "previous_session";
export type ScreenerDataState = "fresh" | "partial" | "stale" | "missing" | "fallback";

export interface ScreenerFreshness {
  fetchedAt: string;
  asOfLabel: string;
  relativeLabel: string;
  cacheHit: boolean;
  source: ScreenerFreshnessSource;
  dataState: ScreenerDataState;
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
}
