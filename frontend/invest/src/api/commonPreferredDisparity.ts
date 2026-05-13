import type { CommonPreferredDisparityResponse } from "../types/commonPreferredDisparity";

export type CommonPreferredDisparityParams = {
  symbols?: string;
  limit?: number;
  maxStaleDays?: number;
  signal?: AbortSignal;
};

export async function fetchCommonPreferredDisparity(
  params: CommonPreferredDisparityParams = {},
): Promise<CommonPreferredDisparityResponse> {
  const q = new URLSearchParams();
  if (params.symbols?.trim()) q.set("symbols", params.symbols.trim());
  if (params.limit !== undefined) q.set("limit", String(params.limit));
  if (params.maxStaleDays !== undefined) q.set("maxStaleDays", String(params.maxStaleDays));
  const suffix = q.toString() ? `?${q.toString()}` : "";
  const res = await fetch(`/invest/api/disparity/common-preferred${suffix}`, {
    credentials: "include",
    signal: params.signal,
  });
  if (!res.ok) throw new Error(`/invest/api/disparity/common-preferred ${res.status}`);
  return res.json();
}
