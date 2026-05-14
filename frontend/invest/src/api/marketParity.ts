import type { MarketParityResponse } from "../types/marketParity";

export async function fetchMarketParity(signal?: AbortSignal): Promise<MarketParityResponse> {
  const res = await fetch("/invest/api/market-parity?market=kr&includeDisabled=true&limit=8", {
    credentials: "include",
    signal,
  });
  if (!res.ok) {
    throw new Error(`/invest/api/market-parity ${res.status}`);
  }
  return res.json();
}
