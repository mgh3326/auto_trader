// frontend/invest/src/types/newsRadar.ts
export type NewsRadarReadinessStatus = "ready" | "stale" | "unavailable";
export type NewsRadarSeverity = "high" | "medium" | "low";
export type NewsRadarRiskCategory =
  | "geopolitical_oil"
  | "macro_policy"
  | "crypto_security"
  | "earnings_bigtech"
  | "korea_market";
export type NewsRadarMarket = "all" | "kr" | "us" | "crypto";

export interface NewsRadarReadiness {
  status: NewsRadarReadinessStatus;
  latest_scraped_at: string | null;
  latest_published_at: string | null;
  recent_6h_count: number;
  recent_24h_count: number;
  source_count: number;
  stale: boolean;
  max_age_minutes: number;
  warnings: string[];
}

export interface NewsRadarSummary {
  high_risk_count: number;
  total_count: number;
  included_in_briefing_count: number;
  excluded_but_collected_count: number;
}

export interface NewsRadarSourceCoverage {
  feed_source: string;
  recent_6h: number;
  recent_24h: number;
  latest_published_at: string | null;
  latest_scraped_at: string | null;
  status: string;
}

export interface NewsRadarItem {
  id: string;
  title: string;
  source: string | null;
  feed_source: string | null;
  url: string;
  published_at: string | null;
  market: string;
  risk_category: NewsRadarRiskCategory | null;
  severity: NewsRadarSeverity;
  themes: string[];
  symbols: string[];
  included_in_briefing: boolean;
  briefing_reason: string | null;
  briefing_score: number;
  snippet: string | null;
  matched_terms: string[];
}

export interface NewsRadarSection {
  section_id: NewsRadarRiskCategory;
  title: string;
  severity: NewsRadarSeverity;
  items: NewsRadarItem[];
}

export interface NewsRadarResponse {
  market: NewsRadarMarket;
  as_of: string;
  readiness: NewsRadarReadiness;
  summary: NewsRadarSummary;
  sections: NewsRadarSection[];
  items: NewsRadarItem[];
  excluded_items: NewsRadarItem[];
  source_coverage: NewsRadarSourceCoverage[];
}
