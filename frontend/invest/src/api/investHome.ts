import type { InvestHomeResponse } from "../types/invest";

export async function fetchInvestHome(signal?: AbortSignal): Promise<InvestHomeResponse> {
  const res = await fetch("/invest/api/home", { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`/invest/api/home ${res.status}`);
  }
  return (await res.json()) as InvestHomeResponse;
}
