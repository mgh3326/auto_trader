import type { FxDashboardResponse } from "../types/fxDashboard";

export async function fetchFxDashboard(signal?: AbortSignal): Promise<FxDashboardResponse> {
  const res = await fetch("/invest/api/market/fx/dashboard", { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`/invest/api/market/fx/dashboard ${res.status}`);
  }
  return res.json();
}
