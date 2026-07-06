export type MarketDashboardState = "fresh" | "partial" | "missing" | "error";
export type MarketDashboardSectionId = "kr_market" | "global_indices" | "fx_macro" | "crypto_market";
export type MarketDashboardTone = "up" | "down" | "flat" | "unknown";

export type MarketDashboardMetric = {
  label: string;
  value: string | null;
  change?: number | null;
  changePct?: number | null;
  tone: MarketDashboardTone;
  unit?: string | null;
  source: string;
  symbol?: string | null;
  href?: string | null;
  stale: boolean;
  warning?: string | null;
  dataState?: string | null;
  dataStateReason?: string | null;
  quoteAsOf?: string | null;
  quoteLagSeconds?: number | null;
};

export type MarketDashboardSection = {
  id: MarketDashboardSectionId;
  title: string;
  subtitle: string;
  reference: string;
  state: MarketDashboardState;
  sourceOfTruth: string;
  updatedAt?: string | null;
  staleAfterMinutes?: number | null;
  metrics: MarketDashboardMetric[];
  warnings: string[];
  notes: string[];
};

export type MarketDashboardResponse = {
  asOf: string;
  state: MarketDashboardState;
  sections: MarketDashboardSection[];
  warnings: string[];
  notes: string[];
};
