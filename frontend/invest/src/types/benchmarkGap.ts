export type BenchmarkProvider =
  | "toss"
  | "naver"
  | "internal"
  | "kis"
  | "upbit"
  | "news_ingestor";

export type SourceRole =
  | "source_of_truth"
  | "broker_authority"
  | "owned_read_model"
  | "reference"
  | "candidate"
  | "benchmark_only"
  | "excluded"
  | "unsupported";

export type CoverageProductStatus =
  | "covered"
  | "partial"
  | "stale"
  | "missing"
  | "candidate_unwired"
  | "benchmark_only"
  | "intentionally_excluded"
  | "unsupported"
  | "blocked_by_auth_or_policy";

export type BenchmarkGapPriority = "P0" | "P1" | "P2" | "P3";

export type BenchmarkGapDataKind =
  | "raw"
  | "snapshot"
  | "derived"
  | "ui_only"
  | "account"
  | "broker_authority"
  | "reference";

export interface BenchmarkGapRow {
  id: string;
  featureArea: string;
  benchmarkProvider: BenchmarkProvider;
  benchmarkSurface: string;
  benchmarkLabelKo: string;
  sourceRole: SourceRole;
  coverageStatus: CoverageProductStatus;
  priority: BenchmarkGapPriority;
  whyNeeded: string;
  nextAction: string;
  benchmarkUrl?: string | null;
  autoTraderSurface?: string | null;
  autoTraderApi?: string | null;
  autoTraderReadModel?: string | null;
  autoTraderTable?: string | null;
  dataKind?: BenchmarkGapDataKind | null;
  freshnessAt?: string | null;
  gapReason?: string | null;
  relatedLinearIssue?: string | null;
  newIssueCandidate: boolean;
  notes: string[];
}

export interface NextSourcingCandidate {
  rowId: string;
  priority: BenchmarkGapPriority;
  featureArea: string;
  benchmarkProvider: BenchmarkProvider;
  gap: string;
  whyItMatters: string;
  currentStatus: CoverageProductStatus;
  nextAction: string;
  currentAutoTrader?: string | null;
  relatedLinearIssue?: string | null;
  newIssueCandidate: boolean;
}

export interface BenchmarkGapMatrixSummary {
  totalRows: number;
  byStatus: Record<string, number>;
  byPriority: Record<string, number>;
  byProvider: Record<string, number>;
}

export interface BenchmarkGapMatrixResponse {
  market: "kr" | "us" | "crypto" | "all";
  asOf: string;
  rows: BenchmarkGapRow[];
  nextCandidates: NextSourcingCandidate[];
  summary: BenchmarkGapMatrixSummary;
  sourcePolicy: string[];
  notes: string[];
}
