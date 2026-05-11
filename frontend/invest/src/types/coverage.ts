export type CoverageState =
  | "fresh"
  | "stale"
  | "partial"
  | "missing"
  | "unsupported"
  | "error"
  | "provider_unwired";

export type CoverageCandidateReadiness =
  | "live"
  | "request_time_only"
  | "fixture_backed_poc"
  | "aggregate_only_blocked"
  | "not_wired";

export type CoverageCandidateKind = "secondary_source" | "reference" | "candidate";

export type CoverageActionPriority = "none" | "low" | "medium" | "high" | "blocked";

export type CoverageActionKind =
  | "none"
  | "monitor"
  | "investigate"
  | "repair_read_model"
  | "backfill_candidate"
  | "scheduler_candidate"
  | "provider_contract_needed"
  | "unsupported_no_action";

export type CoverageApprovalGate =
  | "none"
  | "code_review"
  | "production_db_write_approval"
  | "scheduler_activation_approval"
  | "broker_order_approval";

export interface CoverageActionability {
  priority: CoverageActionPriority;
  action: CoverageActionKind;
  queue?: string | null;
  approvalGates: CoverageApprovalGate[];
  reason?: string | null;
  safeByDefault: boolean;
}

export interface InvestCoverageCounts {
  expected?: number | null;
  fresh: number;
  stale: number;
  missing: number;
  partial: number;
  total: number;
}

export interface CoverageSourceCandidate {
  name: string;
  surface: string;
  kind: CoverageCandidateKind;
  readiness: CoverageCandidateReadiness;
  latestAt?: string | null;
  latestDate?: string | null;
  counts?: InvestCoverageCounts | null;
  warnings: string[];
  notes: string[];
}

export interface InvestCoverageSurface {
  surface: string;
  label: string;
  state: CoverageState;
  market?: string | null;
  sourceOfTruth: string;
  references: string[];
  latestAt?: string | null;
  latestDate?: string | null;
  counts: InvestCoverageCounts;
  staleAfterHours?: number | null;
  warnings: string[];
  notes: string[];
  sourceCandidates: CoverageSourceCandidate[];
  actionability: CoverageActionability;
}

export interface InvestCoverageSymbol {
  symbol: string;
  market: string;
  surfaces: Record<string, CoverageState>;
  latestDates: Record<string, string | null>;
  warnings: string[];
  actionability: CoverageActionability;
}

export interface InvestCoverageResponse {
  market: "kr" | "us" | "crypto" | "all";
  asOf: string;
  tradingDate: string;
  states: CoverageState[];
  surfaces: InvestCoverageSurface[];
  symbols: InvestCoverageSymbol[];
  gaps: string[];
  notes: string[];
}
