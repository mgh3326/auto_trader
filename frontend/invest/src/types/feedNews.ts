import type { MarketIssue } from "./newsIssues";

export type FeedTab = "top" | "latest" | "hot" | "holdings" | "watchlist" | "kr" | "us" | "crypto";
export type RelationKind = "held" | "watchlist" | "both" | "none";
export type FeedNewsScope = "market_wide" | "symbol_specific" | "mixed" | "kr_market_wide";

export interface FeedRelatedSymbolQuote {
  price?: number | null;
  change?: number | null;
  changeRate?: number | null;
  currency?: string | null;
  asOf?: string | null;
}

export interface FeedRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
  relation?: RelationKind;
  matchReason?: string | null;
  matchedTerm?: string | null;
  quote?: FeedRelatedSymbolQuote | null;
}

export interface FeedNewsItem {
  id: number;
  title: string;
  publisher?: string | null;
  feedSource?: string | null;
  publishedAt?: string | null;
  market: "kr" | "us" | "crypto";
  // ROB-172: source/feed origin market, distinct from relatedSymbols[].market (asset market).
  sourceMarket?: "kr" | "us" | "crypto" | null;
  relatedSymbols: FeedRelatedSymbol[];
  issueId?: string | null;
  summarySnippet?: string | null;
  relation: RelationKind;
  url: string;
  // ROB-155 / ROB-169 — additive read-layer classification.
  scope?: FeedNewsScope;
  tags?: string[];
  category?: string | null;
  noiseReason?: string | null;
}

export interface FeedNewsMeta {
  emptyReason?: string | null;
  warnings: string[];
}

export interface FeedNewsResponse {
  tab: FeedTab;
  asOf: string;
  issues: MarketIssue[];
  items: FeedNewsItem[];
  nextCursor?: string | null;
  meta: FeedNewsMeta;
}
