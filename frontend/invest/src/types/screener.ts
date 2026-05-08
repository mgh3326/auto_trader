export type ScreenerMarket = "kr" | "us" | "crypto";
export type ScreenerChangeDirection = "up" | "down" | "flat";

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
  warnings: string[];
}

export interface ScreenerResultsResponse {
  presetId: string;
  title: string;
  description: string;
  filterChips: ScreenerFilterChip[];
  metricLabel: string;
  results: ScreenerResultRow[];
  warnings: string[];
}
