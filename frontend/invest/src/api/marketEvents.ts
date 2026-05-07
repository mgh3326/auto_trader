import type {
  FetchMarketEventsTodayParams,
  MarketEventsDayResponse,
} from "../types/marketEvents";

export async function fetchMarketEventsToday(
  params: FetchMarketEventsTodayParams = {},
  signal?: AbortSignal,
): Promise<MarketEventsDayResponse> {
  const search = new URLSearchParams();
  if (params.category) search.set("category", params.category);
  if (params.market) search.set("market", params.market);
  if (params.source) search.set("source", params.source);
  if (params.onDate) search.set("on_date", params.onDate);
  const qs = search.toString();
  const url = qs
    ? `/trading/api/market-events/today?${qs}`
    : "/trading/api/market-events/today";

  const res = await fetch(url, {
    credentials: "include",
    signal,
  });
  if (!res.ok) {
    throw new Error(`/trading/api/market-events/today ${res.status}`);
  }
  return (await res.json()) as MarketEventsDayResponse;
}
