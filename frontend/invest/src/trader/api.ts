import type {
  TraderFillsResponse,
  TraderOpenOrdersResponse,
  TraderWatchesResponse,
} from "./types";

const BASE = "/trading/api/trader";

export class TraderApiError extends Error {
  constructor(
    public status: number,
    path: string,
  ) {
    super(`trader api ${path} -> ${status}`);
  }
}

function redirectToLogin(): never {
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.href = `/web-auth/login?next=${encodeURIComponent(next)}`;
  throw new TraderApiError(401, "login-redirect");
}

async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`${BASE}${path}`, { credentials: "include" });
  if (res.status === 401) redirectToLogin();
  if (!res.ok) throw new TraderApiError(res.status, path);
  return res.json() as Promise<T>;
}

export async function fetchTraderOpenOrders(
  refresh = false,
): Promise<TraderOpenOrdersResponse> {
  const q = refresh ? "?refresh=1" : "";
  return getJson<TraderOpenOrdersResponse>(`/open-orders${q}`);
}

export async function fetchTraderFillsToday(): Promise<TraderFillsResponse> {
  return getJson<TraderFillsResponse>("/fills/today");
}

export async function fetchTraderWatches(): Promise<TraderWatchesResponse> {
  return getJson<TraderWatchesResponse>("/watches");
}
