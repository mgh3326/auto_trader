import type { FeedNewsResponse, FeedTab, FeedTopic } from "../types/feedNews";

export async function fetchFeedNews(params: {
  tab: FeedTab;
  limit?: number;
  cursor?: string;
  includeQuotes?: boolean;
  topic?: FeedTopic | null;
}): Promise<FeedNewsResponse> {
  const q = new URLSearchParams();
  q.set("tab", params.tab);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.cursor) q.set("cursor", params.cursor);
  if (params.includeQuotes !== undefined) q.set("includeQuotes", String(params.includeQuotes));
  if (params.topic) q.set("topic", params.topic);
  const res = await fetch(`/invest/api/feed/news?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`feed/news ${res.status}`);
  return res.json();
}
