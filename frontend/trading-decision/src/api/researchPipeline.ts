import { apiFetch } from "./client";
import type {
  ResearchSessionCreateRequest,
  ResearchSessionCreateResponse,
  ResearchSessionFullResponse,
  ResearchSessionHeader,
  ResearchSessionListItem,
  ResearchSummary,
  StageAnalysis,
  SymbolTimelineResponse,
} from "./types";

export function listSessions(args: {
  limit?: number;
} = {}): Promise<ResearchSessionListItem[]> {
  const limit = args.limit ?? 20;
  return apiFetch<ResearchSessionListItem[]>(
    `/research-pipeline/sessions?limit=${limit}`,
  );
}

export function createSession(
  body: ResearchSessionCreateRequest,
): Promise<ResearchSessionCreateResponse> {
  const payload: ResearchSessionCreateRequest = {
    triggered_by: "user",
    ...body,
  };
  return apiFetch<ResearchSessionCreateResponse>(
    "/research-pipeline/sessions",
    { method: "POST", body: JSON.stringify(payload) },
  );
}

export function getSession(sessionId: number): Promise<ResearchSessionHeader> {
  return apiFetch<ResearchSessionHeader>(
    `/research-pipeline/sessions/${sessionId}`,
  );
}

export function getSessionFull(
  sessionId: number,
): Promise<ResearchSessionFullResponse> {
  return apiFetch<ResearchSessionFullResponse>(
    `/research-pipeline/sessions/${sessionId}?include=full`,
  );
}

export function getSessionStages(
  sessionId: number,
): Promise<StageAnalysis[]> {
  return apiFetch<StageAnalysis[]>(
    `/research-pipeline/sessions/${sessionId}/stages`,
  );
}

export function getSessionSummary(
  sessionId: number,
): Promise<ResearchSummary> {
  return apiFetch<ResearchSummary>(
    `/research-pipeline/sessions/${sessionId}/summary`,
  );
}

export function getSymbolTimeline(
  symbol: string,
  days = 30,
): Promise<SymbolTimelineResponse> {
  return apiFetch<SymbolTimelineResponse>(
    `/research-pipeline/symbols/${encodeURIComponent(symbol)}/timeline?days=${days}`,
  );
}
