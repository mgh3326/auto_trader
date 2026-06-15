import type { CurrentOrdersMarket, CurrentOrdersResponse } from "../types/currentOrders";

const BASE = "/trading/api/invest/open-orders";

export async function fetchCurrentOrders(
  market: CurrentOrdersMarket = "all",
): Promise<CurrentOrdersResponse> {
  const q = new URLSearchParams({ market });
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`open-orders ${res.status}`);
  return res.json();
}
