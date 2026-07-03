// Read-only analysis artifact surface (ROB-664).
// Mirrors app/schemas/analysis_artifact.py field-for-field (snake_case preserved).

export type ArtifactKind =
  | "screening_ranking"
  | "profit_taking_verdicts"
  | "support_resistance_map"
  | "flow_assessment"
  | "candidate_pool"
  | "session_summary"
  | "briefing";

export type ArtifactReadiness =
  | "screen_grade"
  | "not_decision_ready"
  | "ready_for_order_review"
  | "blocked";

export type ArtifactCreatedBy = "claude" | "operator" | "system";

export interface ArtifactMeta {
  id: number;
  artifact_uuid: string;
  market: "kr" | "us" | "crypto";
  kind: ArtifactKind;
  title: string;
  symbols: string[];
  as_of: string;
  valid_until: string | null;
  session_label: string | null;
  correlation_id: string | null;
  account_scope: string | null;
  content_hash: string | null;
  version: number;
  readiness_label: ArtifactReadiness | null;
  payload_size_bytes: number;
  is_stale: boolean;
  created_by: ArtifactCreatedBy;
  created_at: string;
}

export interface ArtifactRead extends ArtifactMeta {
  payload: Record<string, unknown>;
}

export interface ArtifactListFilters {
  market: "kr" | "us" | "crypto" | null;
  kind: ArtifactKind | null;
  symbol: string | null;
  since: string | null;
  include_stale: boolean;
  limit: number;
  correlation_id: string | null;
  account_scope: string | null;
}

export interface ArtifactListResponse {
  success: true;
  count: number;
  filters: ArtifactListFilters;
  artifacts: ArtifactMeta[];
}

export interface ArtifactGetResponse {
  success: true;
  artifact: ArtifactRead;
}
