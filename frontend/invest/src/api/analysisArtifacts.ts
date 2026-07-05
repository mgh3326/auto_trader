import type {
  ArtifactGetResponse,
  ArtifactKind,
  ArtifactListResponse,
  ArtifactReadiness,
} from "../types/analysisArtifacts";

const BASE = "/trading/api/invest/artifacts";

export interface ArtifactListQuery {
  market?: "kr" | "us" | "crypto";
  kind?: ArtifactKind;
  readinessLabel?: ArtifactReadiness;
  symbol?: string;
  includeStale?: boolean;
  limit?: number;
  correlationIds?: string[];
}

export async function fetchArtifacts(
  params: ArtifactListQuery = {},
): Promise<ArtifactListResponse> {
  const q = new URLSearchParams();
  if (params.market) q.set("market", params.market);
  if (params.kind) q.set("kind", params.kind);
  if (params.readinessLabel) q.set("readiness_label", params.readinessLabel);
  if (params.symbol) q.set("symbol", params.symbol);
  if (params.includeStale) q.set("include_stale", "true");
  if (params.correlationIds) {
    for (const cid of params.correlationIds) q.append("correlation_id", cid);
  }
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  const res = await fetch(`${BASE}/${qs ? `?${qs}` : ""}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`fetchArtifacts failed: ${res.status}`);
  return res.json();
}

export async function fetchArtifactDetail(
  id: number | string,
): Promise<ArtifactGetResponse> {
  const res = await fetch(`${BASE}/${id}`, { credentials: "include" });
  if (!res.ok) throw new Error(`fetchArtifactDetail failed: ${res.status}`);
  return res.json();
}
