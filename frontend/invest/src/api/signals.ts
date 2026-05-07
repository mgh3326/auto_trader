import type { SignalsResponse, SignalTab } from "../types/signals";

export async function fetchSignals(params: {
  tab: SignalTab;
  limit?: number;
}): Promise<SignalsResponse> {
  const q = new URLSearchParams();
  q.set("tab", params.tab);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  const res = await fetch(`/invest/api/signals?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`signals ${res.status}`);
  return res.json();
}
