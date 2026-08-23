import type { BuyPlanMarketFilter, BuyPlanResponse } from "../types/buyPlan";

const BASE = "/trading/api/invest/buy-plan";

export async function fetchBuyPlan(
  market: BuyPlanMarketFilter = "all",
): Promise<BuyPlanResponse> {
  const q = new URLSearchParams({ market });
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`buy-plan ${res.status}`);
  return res.json();
}
