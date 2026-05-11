export type FeedResearchTab = "top" | "latest" | "mine" | "watchlist" | "holdings" | "kr" | "us";
export type ResearchRelation = "mine" | "watch" | "none";
export type ResearchMarket = "kr" | "us" | "crypto";

export interface ResearchSymbolCandidate {
  symbol: string;
  market?: ResearchMarket | string | null;
  name?: string | null;
  displayName?: string | null;
  confidence?: number | null;
  source?: string | null;
  reason?: string | null;
}

export interface FeedResearchItem {
  id: number;
  source: string;
  title?: string | null;
  analyst?: string | null;
  publishedAtText?: string | null;
  publishedAt?: string | null;
  category?: string | null;
  detailUrl?: string | null;
  pdfUrl?: string | null;
  excerpt?: string | null;
  symbolCandidates: ResearchSymbolCandidate[];
  attributionPublisher?: string | null;
  attributionCopyrightNotice?: string | null;
  market?: ResearchMarket | null;
  relation: ResearchRelation;
}

export interface FeedResearchAppliedFilters {
  source?: string | null;
  symbol?: string | null;
  analyst?: string | null;
  category?: string | null;
  query?: string | null;
  fromDate?: string | null;
  toDate?: string | null;
}

export interface FeedResearchMeta {
  limit: number;
  appliedFilters: FeedResearchAppliedFilters;
}

export interface FeedResearchResponse {
  tab: FeedResearchTab;
  asOf: string;
  items: FeedResearchItem[];
  nextCursor?: string | null;
  meta: FeedResearchMeta;
}
