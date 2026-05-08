import type { FeedNewsResponse, FeedTab } from "../types/feedNews";

export async function fetchFeedNews(params: {
  tab: FeedTab;
  limit?: number;
  cursor?: string;
}): Promise<FeedNewsResponse> {
  const q = new URLSearchParams();
  q.set("tab", params.tab);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.cursor) q.set("cursor", params.cursor);
  const res = await fetch(`/invest/api/feed/news?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`feed/news ${res.status}`);
  return res.json();
}
