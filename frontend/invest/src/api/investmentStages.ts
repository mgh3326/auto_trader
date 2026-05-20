// ROB-279 Phase 5 — API client for the stage artifacts endpoint.
//
// GET /invest/api/investment-reports/{reportUuid}/stage-artifacts
// Converts the snake_case JSON to camelCase TypeScript shapes from
// ``../types/investmentReports``.

import type {
  ReportStageArtifactsResponse,
  StageArtifact,
  StageType,
  StageVerdict,
} from "../types/investmentReports";

const STAGE_ARTIFACTS_ENDPOINT = (reportUuid: string) =>
  `/invest/api/investment-reports/${encodeURIComponent(reportUuid)}/stage-artifacts`;

type ApiArtifact = Record<string, unknown>;

function asString(value: unknown, fallback: string = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asOptionalString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  return typeof value === "string" ? value : null;
}

function asNumber(value: unknown, fallback: number = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function asArray<T = unknown>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function asOptionalRecord(
  value: unknown,
): Record<string, unknown> | null {
  if (value === null || value === undefined) return null;
  if (typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, unknown>;
  }
  return null;
}

function normalizeArtifact(raw: ApiArtifact): StageArtifact {
  return {
    artifactUuid: asString(raw.artifact_uuid),
    runUuid: asString(raw.run_uuid),
    stageType: asString(raw.stage_type, "market") as StageType,
    verdict: asString(raw.verdict, "unavailable") as StageVerdict,
    confidence: asNumber(raw.confidence),
    summary: asOptionalString(raw.summary),
    keyPoints: asArray(raw.key_points),
    buyEvidence: asArray(raw.buy_evidence),
    sellEvidence: asArray(raw.sell_evidence),
    riskEvidence: asArray(raw.risk_evidence),
    missingData: asArray(raw.missing_data),
    citedSnapshotUuids: asArray<string>(raw.cited_snapshot_uuids),
    freshnessSummary: asOptionalRecord(raw.freshness_summary),
    modelName: asOptionalString(raw.model_name),
    promptVersion: asOptionalString(raw.prompt_version),
    payloadHash: asOptionalString(raw.payload_hash),
    rawPayloadJson: asOptionalRecord(raw.raw_payload_json),
    createdAt: asString(raw.created_at),
  };
}

export async function fetchReportStageArtifacts(
  reportUuid: string,
  signal?: AbortSignal,
): Promise<ReportStageArtifactsResponse> {
  const endpoint = STAGE_ARTIFACTS_ENDPOINT(reportUuid);
  const res = await fetch(endpoint, { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`${endpoint} ${res.status}`);
  }
  const raw = (await res.json()) as {
    report_uuid?: unknown;
    stage_run_uuid?: unknown;
    artifacts?: ApiArtifact[];
  };

  return {
    reportUuid: asString(raw.report_uuid),
    stageRunUuid: asOptionalString(raw.stage_run_uuid),
    artifacts: asArray<ApiArtifact>(raw.artifacts).map(normalizeArtifact),
  };
}
