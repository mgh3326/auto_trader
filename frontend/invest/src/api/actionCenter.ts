import type { AnalysisCandidate, AnalysisCandidateQueueResponse, AnalysisReport, AnalysisReportListResponse, AnalysisStageResult } from "../types/actionCenter";

const REPORTS_ENDPOINT = "/invest/api/action-center/reports";
const CANDIDATES_ENDPOINT = "/invest/api/action-center/candidates";
const UNAVAILABLE_LABEL = "확인 불가";

type ApiListResponse<T> = { count?: number; items?: T[] };

type ApiStageResult = Partial<AnalysisStageResult> & {
  stage_key?: string;
  freshness_at?: string | null;
  unavailable_reason?: string | null;
};

type ApiCandidate = Partial<AnalysisCandidate> & {
  candidate_uuid?: string;
  report_uuid?: string | null;
  action_type?: string;
  quantity_pct?: number | string | null;
  limit_price?: number | string | null;
  risk_notes?: string[];
  blocking_reasons?: string[];
  approval_status?: string;
  approval_type?: string;
  execution_state?: string;
  created_at?: string;
  valid_until?: string | null;
};

type ApiReport = Partial<AnalysisReport> & {
  report_uuid?: string;
  report_type?: string;
  account_scope?: string | null;
  created_by_profile?: string;
  risk_summary?: string | null;
  data_freshness?: Record<string, unknown>;
  source_policy?: string[];
  safety_notes?: string[];
  created_at?: string;
  published_at?: string | null;
  valid_until?: string | null;
  stages?: ApiStageResult[];
};

async function readJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(endpoint, { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`${endpoint} ${res.status}`);
  }
  return res.json();
}

function normalizeStage(stage: ApiStageResult): AnalysisStageResult {
  return {
    stageKey: stage.stageKey ?? stage.stage_key ?? UNAVAILABLE_LABEL,
    source: stage.source ?? UNAVAILABLE_LABEL,
    status: stage.status ?? "unavailable",
    freshnessAt: stage.freshnessAt ?? stage.freshness_at ?? null,
    unavailableReason: stage.unavailableReason ?? stage.unavailable_reason ?? null,
    warnings: stage.warnings ?? [],
  };
}

function normalizeCandidate(candidate: ApiCandidate): AnalysisCandidate {
  return {
    candidateUuid: candidate.candidateUuid ?? candidate.candidate_uuid ?? UNAVAILABLE_LABEL,
    reportUuid: candidate.reportUuid ?? candidate.report_uuid ?? null,
    symbol: candidate.symbol ?? UNAVAILABLE_LABEL,
    market: candidate.market ?? UNAVAILABLE_LABEL,
    side: candidate.side ?? "buy",
    actionType: candidate.actionType ?? candidate.action_type ?? UNAVAILABLE_LABEL,
    quantity: candidate.quantity ?? null,
    quantityPct: candidate.quantityPct ?? candidate.quantity_pct ?? null,
    limitPrice: candidate.limitPrice ?? candidate.limit_price ?? null,
    notional: candidate.notional ?? null,
    currency: candidate.currency ?? null,
    priority: candidate.priority ?? 0,
    confidence: candidate.confidence ?? null,
    thesis: candidate.thesis ?? UNAVAILABLE_LABEL,
    riskNotes: candidate.riskNotes ?? candidate.risk_notes ?? [],
    verification: candidate.verification ?? {},
    blockingReasons: candidate.blockingReasons ?? candidate.blocking_reasons ?? [],
    approvalStatus: candidate.approvalStatus ?? candidate.approval_status ?? UNAVAILABLE_LABEL,
    approvalType: candidate.approvalType ?? candidate.approval_type ?? UNAVAILABLE_LABEL,
    executionState: candidate.executionState ?? candidate.execution_state ?? "not_submitted",
    createdAt: candidate.createdAt ?? candidate.created_at ?? UNAVAILABLE_LABEL,
    validUntil: candidate.validUntil ?? candidate.valid_until ?? null,
  };
}

function normalizeReport(report: ApiReport): AnalysisReport {
  return {
    reportUuid: report.reportUuid ?? report.report_uuid ?? UNAVAILABLE_LABEL,
    reportType: report.reportType ?? report.report_type ?? UNAVAILABLE_LABEL,
    market: report.market ?? UNAVAILABLE_LABEL,
    accountScope: report.accountScope ?? report.account_scope ?? null,
    createdByProfile: report.createdByProfile ?? report.created_by_profile ?? UNAVAILABLE_LABEL,
    status: report.status ?? "draft",
    summary: report.summary ?? UNAVAILABLE_LABEL,
    riskSummary: report.riskSummary ?? report.risk_summary ?? null,
    dataFreshness: report.dataFreshness ?? report.data_freshness ?? {},
    coverage: report.coverage ?? {},
    sourcePolicy: report.sourcePolicy ?? report.source_policy ?? [],
    safetyNotes: report.safetyNotes ?? report.safety_notes ?? [],
    createdAt: report.createdAt ?? report.created_at ?? UNAVAILABLE_LABEL,
    publishedAt: report.publishedAt ?? report.published_at ?? null,
    validUntil: report.validUntil ?? report.valid_until ?? null,
    stageResults: (report.stageResults ?? report.stages ?? []).map(normalizeStage),
    candidates: (report.candidates ?? []).map(normalizeCandidate),
  };
}

export async function fetchActionCenterReports(signal?: AbortSignal): Promise<AnalysisReportListResponse> {
  const payload = await readJson<AnalysisReportListResponse | ApiListResponse<ApiReport>>(REPORTS_ENDPOINT, signal);
  if ("reports" in payload && Array.isArray(payload.reports)) {
    return { ...payload, reports: payload.reports.map(normalizeReport), unavailableLabel: payload.unavailableLabel ?? UNAVAILABLE_LABEL };
  }
  const listPayload = payload as ApiListResponse<ApiReport>;
  return {
    reports: (listPayload.items ?? []).map(normalizeReport),
    unavailableLabel: UNAVAILABLE_LABEL,
  };
}

export async function fetchActionCenterReport(reportUuid: string, signal?: AbortSignal): Promise<AnalysisReport> {
  const payload = await readJson<ApiReport>(`${REPORTS_ENDPOINT}/${encodeURIComponent(reportUuid)}`, signal);
  return normalizeReport(payload);
}

export async function fetchActionCenterCandidates(signal?: AbortSignal): Promise<AnalysisCandidateQueueResponse> {
  const payload = await readJson<AnalysisCandidateQueueResponse | ApiListResponse<ApiCandidate>>(CANDIDATES_ENDPOINT, signal);
  if ("candidates" in payload && Array.isArray(payload.candidates)) {
    return { ...payload, candidates: payload.candidates.map(normalizeCandidate), unavailableLabel: payload.unavailableLabel ?? UNAVAILABLE_LABEL };
  }
  const listPayload = payload as ApiListResponse<ApiCandidate>;
  return {
    candidates: (listPayload.items ?? []).map(normalizeCandidate),
    unavailableLabel: UNAVAILABLE_LABEL,
  };
}
