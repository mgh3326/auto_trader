import type { InvestCoverageResponse } from "../types/coverage";

export async function fetchInvestCoverage(params: {
  market?: "kr" | "us" | "crypto" | "all";
  symbols?: string;
  asOf?: string;
  signal?: AbortSignal;
} = {}): Promise<InvestCoverageResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.symbols?.trim()) q.set("symbols", params.symbols.trim());
  if (params.asOf) q.set("asOf", params.asOf);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const res = await fetch(`/invest/api/coverage${suffix}`, {
    credentials: "include",
    signal: params.signal,
  });
  if (!res.ok) {
    throw new Error(`/invest/api/coverage ${res.status}`);
  }
  return res.json();
}
