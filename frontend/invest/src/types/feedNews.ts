import type { MarketIssue } from "./newsIssues";

export type FeedTab = "top" | "latest" | "hot" | "holdings" | "watchlist" | "kr" | "us" | "crypto";
export type FeedTopic = "fx" | "rates";
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
  /**
   * ROB-172: the *asset's* market (e.g. NVDA → "us"), not the article's
   * source market. May differ from FeedNewsItem.sourceMarket when an article
   * in one market discusses a symbol listed in another market.
   */
  market: "kr" | "us" | "crypto";
  displayName: string;
  relation?: RelationKind;
  matchReason?: string | null;
  matchedTerm?: string | null;
  quote?: FeedRelatedSymbolQuote | null;
  currentPrice?: number | null;
  previousClose?: number | null;
  change?: number | null;
  changePct?: number | null;
  quoteSource?: string | null;
  quoteAsOf?: string | null;
}

export interface FeedNewsItem {
  id: number;
  title: string;
  publisher?: string | null;
  feedSource?: string | null;
  publishedAt?: string | null;
  /**
   * Source/feed market of the article (kr/us/crypto).
   * Backward-compatible alias for `sourceMarket`; kept so existing readers
   * need no change during the migration window. Prefer `sourceMarket` for
   * new code. Do NOT use this to infer the market of a related symbol —
   * use `FeedRelatedSymbol.market` for that.
   */
  market: "kr" | "us" | "crypto";
  /**
   * ROB-172: source/feed market of the article (kr/us/crypto).
   * Equal to `market` during the backward-compat window. New code should
   * read this field instead of `market` so the naming is unambiguous when
   * compared with `FeedRelatedSymbol.market` (the asset's market).
   * Nullable to tolerate older backend payloads that pre-date the dual emit.
   */
  sourceMarket?: "kr" | "us" | "crypto" | null;
  relatedSymbols: FeedRelatedSymbol[];
  issueId?: string | null;
  summarySnippet?: string | null;
  relation: RelationKind;
  url: string;
  // ROB-155 / ROB-169 / ROB-220 — additive read-layer classification.
  scope?: FeedNewsScope;
  tags?: string[];
  topicTags?: FeedTopic[];
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
