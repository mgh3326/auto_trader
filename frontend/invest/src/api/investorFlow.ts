import type { InvestorFlowResponse } from "../types/investorFlow";

export async function fetchInvestorFlow(params: {
  symbols: string[];
  market?: "kr";
  asOf?: string;
  maxStaleDays?: number;
  signal?: AbortSignal;
}): Promise<InvestorFlowResponse> {
  const q = new URLSearchParams();
  q.set("symbols", params.symbols.join(","));
  q.set("market", params.market ?? "kr");
  if (params.asOf) q.set("asOf", params.asOf);
  if (params.maxStaleDays !== undefined) q.set("maxStaleDays", String(params.maxStaleDays));

  const res = await fetch(`/invest/api/investor-flow?${q}`, {
    credentials: "include",
    signal: params.signal,
  });
  if (!res.ok) throw new Error(`investor-flow ${res.status}`);
  return res.json();
}
