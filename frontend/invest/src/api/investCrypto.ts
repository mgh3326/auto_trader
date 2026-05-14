import type { CryptoDashboardResponse, NaverCryptoReferenceResponse } from "../types/investCrypto";

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url, { credentials: "include" });
  if (!res.ok) throw new Error(`${url} ${res.status}`);
  return res.json();
}

export async function fetchCryptoDashboard(params: { limit?: number } = {}): Promise<CryptoDashboardResponse> {
  const q = new URLSearchParams();
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  const qs = q.toString();
  return getJson<CryptoDashboardResponse>(`/invest/api/crypto/dashboard${qs ? `?${qs}` : ""}`);
}

export async function fetchCryptoNaverReference(
  params: { symbol?: string; limit?: number } = {},
): Promise<NaverCryptoReferenceResponse> {
  const q = new URLSearchParams();
  if (params.symbol) q.set("symbol", params.symbol);
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  const qs = q.toString();
  return getJson<NaverCryptoReferenceResponse>(`/invest/api/crypto/naver-reference${qs ? `?${qs}` : ""}`);
}
