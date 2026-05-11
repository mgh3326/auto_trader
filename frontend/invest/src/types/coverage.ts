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
}

export interface InvestCoverageSymbol {
  symbol: string;
  market: string;
  surfaces: Record<string, CoverageState>;
  latestDates: Record<string, string | null>;
  warnings: string[];
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
