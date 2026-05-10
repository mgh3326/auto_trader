import type { FeedResearchResponse, FeedResearchTab } from "../types/feedResearch";

export async function fetchFeedResearch(params: {
  tab: FeedResearchTab;
  limit?: number;
  cursor?: string;
  source?: string;
  symbol?: string;
  analyst?: string;
  category?: string;
  query?: string;
  fromDate?: string;
  toDate?: string;
}): Promise<FeedResearchResponse> {
  const q = new URLSearchParams();
  q.set("tab", params.tab);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.cursor) q.set("cursor", params.cursor);
  if (params.source) q.set("source", params.source);
  if (params.symbol) q.set("symbol", params.symbol);
  if (params.analyst) q.set("analyst", params.analyst);
  if (params.category) q.set("category", params.category);
  if (params.query) q.set("query", params.query);
  if (params.fromDate) q.set("fromDate", params.fromDate);
  if (params.toDate) q.set("toDate", params.toDate);
  const res = await fetch(`/invest/api/feed/research?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`feed/research ${res.status}`);
  return res.json();
}
