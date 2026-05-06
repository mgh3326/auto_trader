// frontend/trading-decision/src/api/researchRetrospective.ts
import { apiFetch } from "./client";
import type {
  Market,
  RetrospectiveDecisionsResponse,
  RetrospectiveOverview,
  RetrospectiveStagePerformanceRow,
} from "./types";

export interface RetrospectiveFilters {
  days?: number;
  market?: Market;
  strategy?: string;
}

function buildQs(filters: Record<string, unknown>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(filters)) {
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export function getRetrospectiveOverview(
  filters: RetrospectiveFilters = {},
): Promise<RetrospectiveOverview> {
  return apiFetch<RetrospectiveOverview>(
    `/research-retrospective/overview${buildQs({ ...filters })}`,
  );
}

export function getRetrospectiveStagePerformance(
  filters: RetrospectiveFilters = {},
): Promise<RetrospectiveStagePerformanceRow[]> {
  return apiFetch<RetrospectiveStagePerformanceRow[]>(
    `/research-retrospective/stage-performance${buildQs({ ...filters })}`,
  );
}

export function listRetrospectiveDecisions(
  filters: RetrospectiveFilters & { limit?: number } = {},
): Promise<RetrospectiveDecisionsResponse> {
  return apiFetch<RetrospectiveDecisionsResponse>(
    `/research-retrospective/decisions${buildQs({ ...filters })}`,
  );
}
