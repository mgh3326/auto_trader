// frontend/invest/src/types/newsIssues.ts
export type MarketIssueMarket = "kr" | "us" | "crypto";
export type MarketIssuesMarketFilter = MarketIssueMarket | "all";
export type IssueDirection = "up" | "down" | "mixed" | "neutral";

export interface IssueSignals {
  recency_score: number;
  source_diversity_score: number;
  mention_score: number;
}

export interface MarketIssueArticle {
  id: number;
  title: string;
  url: string;
  source: string | null;
  feed_source: string | null;
  published_at: string | null;
  summary: string | null;
  matched_terms: string[];
}

export interface MarketIssueRelatedSymbol {
  symbol: string;
  market: string;
  canonical_name: string;
  mention_count: number;
}

export interface MarketIssue {
  id: string;
  market: MarketIssueMarket;
  rank: number;
  issue_title: string;
  subtitle: string | null;
  direction: IssueDirection;
  source_count: number;
  article_count: number;
  updated_at: string;
  summary: string | null;
  related_symbols: MarketIssueRelatedSymbol[];
  related_sectors: string[];
  articles: MarketIssueArticle[];
  signals: IssueSignals;
}

export interface MarketIssuesResponse {
  market: MarketIssuesMarketFilter;
  as_of: string;
  window_hours: number;
  items: MarketIssue[];
}
