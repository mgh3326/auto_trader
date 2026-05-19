import type { BenchmarkGapMatrixResponse } from "../types/benchmarkGap";

export async function fetchBenchmarkGapMatrix(
  params: {
    market?: "kr" | "us" | "crypto" | "all";
    asOf?: string;
    signal?: AbortSignal;
  } = {},
): Promise<BenchmarkGapMatrixResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.asOf) q.set("asOf", params.asOf);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const res = await fetch(`/invest/api/coverage/benchmark-gap${suffix}`, {
    credentials: "include",
    signal: params.signal,
  });
  if (!res.ok) {
    throw new Error(`/invest/api/coverage/benchmark-gap ${res.status}`);
  }
  return res.json();
}
