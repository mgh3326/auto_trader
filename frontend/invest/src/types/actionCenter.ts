export type ActionCenterStatus = "loading" | "ready" | "error";

export interface AnalysisStageResult {
  stageKey: string;
  source: string;
  status: string;
  freshnessAt?: string | null;
  unavailableReason?: string | null;
  warnings?: string[];
}

export interface AnalysisCandidate {
  candidateUuid: string;
  reportUuid?: string | null;
  symbol: string;
  market: string;
  side: "buy" | "sell" | string;
  actionType: string;
  quantity?: number | string | null;
  quantityPct?: number | string | null;
  limitPrice?: number | string | null;
  notional?: number | string | null;
  currency?: string | null;
  priority: number;
  confidence?: number | string | null;
  thesis: string;
  riskNotes: string[];
  verification: Record<string, unknown>;
  blockingReasons: string[];
  approvalStatus: string;
  approvalType: string;
  executionState: string;
  createdAt: string;
  validUntil?: string | null;
}

export interface AnalysisReport {
  reportUuid: string;
  reportType: string;
  market: string;
  accountScope?: string | null;
  createdByProfile: string;
  status: string;
  summary: string;
  riskSummary?: string | null;
  dataFreshness: Record<string, unknown>;
  coverage: Record<string, unknown>;
  sourcePolicy?: string[];
  safetyNotes?: string[];
  createdAt: string;
  publishedAt?: string | null;
  validUntil?: string | null;
  stageResults?: AnalysisStageResult[];
  candidates?: AnalysisCandidate[];
}

export interface AnalysisReportListResponse {
  reports: AnalysisReport[];
  unavailableLabel?: string;
}

export interface AnalysisCandidateQueueResponse {
  candidates: AnalysisCandidate[];
  unavailableLabel?: string;
}
