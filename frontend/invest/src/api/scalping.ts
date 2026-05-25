// ROB-315 Phase 3 — /invest/api/scalping client (read + review).
import type {
  ScalpingProduct,
  ScalpingReviewDetailResponse,
  ScalpingReviewListResponse,
  ScalpingTradesResponse,
} from "../types/scalping";

const BASE = "/invest/api/scalping";

async function getJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`${url} ${res.status}`);
  }
  return res.json();
}

export async function fetchScalpingReviews(params: {
  date?: string;
  product?: ScalpingProduct;
  signal?: AbortSignal;
} = {}): Promise<ScalpingReviewListResponse> {
  const q = new URLSearchParams();
  if (params.date) q.set("date", params.date);
  if (params.product) q.set("product", params.product);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return getJson(`${BASE}/reviews${suffix}`, params.signal);
}

export async function fetchScalpingReview(
  reviewId: number,
  signal?: AbortSignal,
): Promise<ScalpingReviewDetailResponse> {
  return getJson(`${BASE}/reviews/${reviewId}`, signal);
}

export async function fetchScalpingTrades(params: {
  date: string;
  product: ScalpingProduct;
  signal?: AbortSignal;
}): Promise<ScalpingTradesResponse> {
  const q = new URLSearchParams({ date: params.date, product: params.product });
  return getJson(`${BASE}/analytics?${q.toString()}`, params.signal);
}

export async function buildScalpingDraft(params: {
  reviewDate: string;
  product: ScalpingProduct;
  sessionTag?: string;
}): Promise<ScalpingReviewDetailResponse["review"]> {
  const res = await fetch(`${BASE}/reviews/draft`, {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      review_date: params.reviewDate,
      product: params.product,
      session_tag: params.sessionTag ?? "",
    }),
  });
  if (!res.ok) {
    throw new Error(`${BASE}/reviews/draft ${res.status}`);
  }
  return (await res.json()).review;
}
