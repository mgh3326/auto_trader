import type { KrActionReadinessResponse } from "../types/actionReadiness";

export async function fetchKrActionReadiness(params: {
  symbol?: string;
  signal?: AbortSignal;
} = {}): Promise<KrActionReadinessResponse> {
  const q = new URLSearchParams();
  if (params.symbol?.trim()) q.set("symbol", params.symbol.trim());
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const res = await fetch(`/invest/api/kr/action-readiness${suffix}`, {
    credentials: "include",
    signal: params.signal,
  });
  if (!res.ok) {
    throw new Error(`/invest/api/kr/action-readiness ${res.status}`);
  }
  return res.json();
}
