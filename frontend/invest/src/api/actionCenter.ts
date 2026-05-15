import type { AnalysisCandidateQueueResponse, AnalysisReport, AnalysisReportListResponse } from "../types/actionCenter";

const REPORTS_ENDPOINT = "/invest/api/action-center/reports";
const CANDIDATES_ENDPOINT = "/invest/api/action-center/candidates";

async function readJson<T>(endpoint: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(endpoint, { credentials: "include", signal });
  if (!res.ok) {
    throw new Error(`${endpoint} ${res.status}`);
  }
  return res.json();
}

export async function fetchActionCenterReports(signal?: AbortSignal): Promise<AnalysisReportListResponse> {
  return readJson<AnalysisReportListResponse>(REPORTS_ENDPOINT, signal);
}

export async function fetchActionCenterReport(reportUuid: string, signal?: AbortSignal): Promise<AnalysisReport> {
  return readJson<AnalysisReport>(`${REPORTS_ENDPOINT}/${encodeURIComponent(reportUuid)}`, signal);
}

export async function fetchActionCenterCandidates(signal?: AbortSignal): Promise<AnalysisCandidateQueueResponse> {
  return readJson<AnalysisCandidateQueueResponse>(CANDIDATES_ENDPOINT, signal);
}
