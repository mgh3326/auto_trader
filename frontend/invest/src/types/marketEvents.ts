export type MarketEventCategory =
  | "earnings"
  | "economic"
  | "disclosure"
  | "crypto_exchange_notice"
  | "crypto_protocol"
  | "tokenomics"
  | "regulatory";

export type MarketEventMarket = "us" | "kr" | "crypto" | "global";

export interface MarketEventValue {
  metric_name: string;
  period: string | null;
  actual: string | null;
  forecast: string | null;
  previous: string | null;
  revised_previous: string | null;
  unit: string | null;
  surprise: string | null;
  surprise_pct: string | null;
  released_at: string | null;
}

export interface MarketEvent {
  category: MarketEventCategory;
  market: MarketEventMarket;
  country: string | null;
  currency: string | null;
  symbol: string | null;
  company_name: string | null;
  title: string | null;
  event_date: string;
  release_time_utc: string | null;
  time_hint: string | null;
  importance: number | null;
  status: string;
  source: string;
  source_event_id: string | null;
  source_url: string | null;
  fiscal_year: number | null;
  fiscal_quarter: number | null;
  held: boolean | null;
  watched: boolean | null;
  values: MarketEventValue[];
}

export interface MarketEventsDayResponse {
  date: string;
  events: MarketEvent[];
}

export interface FetchMarketEventsTodayParams {
  category?: MarketEventCategory;
  market?: MarketEventMarket;
  source?: string;
  /** ISO date — when omitted the backend defaults to today (server clock). */
  onDate?: string;
}
