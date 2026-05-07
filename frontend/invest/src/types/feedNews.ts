import type { MarketIssue } from "./newsIssues";

export type FeedTab = "top" | "latest" | "hot" | "holdings" | "watchlist" | "kr" | "us" | "crypto";
export type RelationKind = "held" | "watchlist" | "both" | "none";

export interface FeedRelatedSymbol {
  symbol: string;
  market: "kr" | "us" | "crypto";
  displayName: string;
}

export interface FeedNewsItem {
  id: number;
  title: string;
  publisher?: string | null;
  feedSource?: string | null;
  publishedAt?: string | null;
  market: "kr" | "us" | "crypto";
  relatedSymbols: FeedRelatedSymbol[];
  issueId?: string | null;
  summarySnippet?: string | null;
  relation: RelationKind;
  url: string;
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
